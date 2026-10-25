"""Fictional shared-question concurrency and Markdown publication tests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from question_pool import SharedQuestionPool
from store import ReviewError, now
from test_store import catalog
from workflow import WorkflowStore


class QuestionPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WorkflowStore(self.root / 'review.sqlite3')
        self.store.import_catalog(catalog(), 'fictional')
        self.pool = SharedQuestionPool(self.store).initialize()
        self.processes = []
        for key in ['a:1', 'a:2']:
            self.add_application(key)

    def add_application(self, key):
        number = len(self.processes)
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key=?", (key,))
            pid = self.store._create_application(db, self.store._row(db, 'jobs', 'key', key), f'approve-{number}')
        task = self.store.lease_task(f'fixture-{number}', ['apply'])
        self.store.start_application(pid, task['lease_token'])
        qid = f'q{number}'
        self.store.checkpoint_task(task['id'], task['lease_token'], {'question_bindings': {qid: 'authorization'}})
        self.store.ask_questions(pid, task['lease_token'], f'form-{number}', [{
            'id': qid, 'text': 'Are you authorized to work in the United States?',
            'type': 'single_choice', 'required': True,
            'choices': [{'id': f'yes-{number}', 'label': 'Yes'}, {'id': f'no-{number}', 'label': 'No'}]}])
        self.processes.append((pid, task['id'], qid))

    def payload(self, index=0, value=None, request='answer-1'):
        pid, _, qid = self.processes[index]
        with self.store.transaction() as db:
            process = self.store._process(db, pid)
            question = db.execute('SELECT * FROM wf_questions WHERE id=?', (qid,)).fetchone()
            return {'request_id': request, 'process_id': pid, 'process_revision': process['revision'],
                'form_version': process['form_version'], 'answers': [{'question_id': qid,
                'question_revision': question['revision'], 'value': value or f'yes-{index}'}]}

    def register(self, index=0, match=None):
        pid, _, qid = self.processes[index]
        return self.pool.register_questions(pid, f'apply-{index + 1}', [qid], matches={qid: match} if match else {})

    def save(self, spec=None):
        spec = spec or self.pool.formatting_spec(self.payload())
        with self.pool.hold('answer_handler', 'user'):
            return self.pool.save_answered_questions(spec['append_text'])

    def test_pending_markdown_moves_to_answered_and_maps_choice_ids(self):
        with self.pool.hold('worker', 'apply-1'):
            first = self.register()['mappings']['q0']
            self.register(1, first)
            self.assertEqual(len(self.pool.pending_questions()), 1)
            initial = self.pool.answer_snapshot()['manifest']['digest']
        self.assertIn('Are you authorized', (self.pool.directory / 'pending-questions.md').read_text())
        pending_markdown = (self.pool.directory / 'pending-questions.md').read_text()
        metadata = json.loads(pending_markdown.split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertEqual(metadata['type'], 'single_choice')
        self.assertTrue(metadata['required'])
        self.assertEqual(metadata['choices'], [{'id': 'yes-0', 'label': 'Yes'}, {'id': 'no-0', 'label': 'No'}])
        self.assertEqual({link['question_id'] for link in metadata['links']}, {'q0', 'q1'})
        self.assertEqual({link['process_id'] for link in metadata['links']}, {p[0] for p in self.processes})
        self.assertEqual({link['worker_id'] for link in metadata['links']}, {'apply-1', 'apply-2'})
        self.assertEqual({link['form_version'] for link in metadata['links']}, {'form-0', 'form-1'})
        spec = self.pool.formatting_spec(self.payload())
        with self.pool.hold('answer_handler', 'user'):
            result = self.pool.save_answered_questions(spec['append_text'])
            self.assertFalse(result['already_answered'])
            self.assertEqual(result['answer_requests'][0]['payload']['answers'][0]['value'], 'yes-1')
            propagated = self.pool.propagate_answers()
            self.assertEqual(propagated['propagated_process_ids'], [self.processes[1][0]])
            self.assertEqual(self.pool.pending_questions(), [])
            snap = self.pool.answer_snapshot(initial)
            self.assertTrue(snap['changed'])
            self.assertIn('Are you authorized', snap['markdown'])
            self.assertNotEqual(snap['manifest']['digest'], initial)
        self.assertNotIn('Are you authorized', (self.pool.directory / 'pending-questions.md').read_text())
        self.assertEqual([t['status'] for t in self.store.tasks()], ['queued', 'queued'])

    def test_single_input_requires_hold_and_rejects_model_changes(self):
        spec = self.pool.formatting_spec(self.payload())
        with self.assertRaises(ReviewError):
            self.pool.save_answered_questions(spec['append_text'])
        with self.pool.hold('answer_handler', 'user'):
            with self.assertRaises(ReviewError):
                self.pool.save_answered_questions(spec['append_text'].replace('["Yes"]', '["No"]'))
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT answer FROM wf_questions WHERE id='q0'").fetchone()[0])

    def test_duplicate_save_does_not_append_or_rehash(self):
        spec = self.pool.formatting_spec(self.payload())
        first = self.save(spec)
        with patch('question_pool._hash_bytes', side_effect=AssertionError('do not rehash unchanged answers')):
            second = self.save(spec)
            with self.pool.hold('worker', 'apply-2'):
                snap = self.pool.answer_snapshot(first['answer_file']['digest'])
                self.assertFalse(snap['changed'])
                self.assertNotIn('markdown', snap)
        self.assertEqual(first['answer_file'], second['answer_file'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0], 1)

    def test_worker_waits_for_answer_then_observes_changed_hash(self):
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            old_hash = self.pool.answer_snapshot()['manifest']['digest']
        spec = self.pool.formatting_spec(self.payload())
        started, done = threading.Event(), threading.Event()
        observations, errors = [], []
        def worker():
            started.set()
            try:
                with self.pool.hold('worker', 'apply-2'):
                    snapshot = self.pool.answer_snapshot(old_hash)
                    observations.append(snapshot['changed'])
                    observations.append(self.register(1, pool_id)['answer_requests'][0]['payload']['answers'][0]['value'])
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
        with self.pool.hold('answer_handler', 'user'):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(done.wait(.05))
            self.pool.save_answered_questions(spec['append_text'])
        thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(observations, [True, 'yes-1'])

    def test_two_matching_workers_are_serialized(self):
        order, errors = [], []
        barrier = threading.Barrier(2)
        def worker(index):
            try:
                barrier.wait()
                with self.pool.hold('worker', f'apply-{index+1}'):
                    existing = self.pool.pending_questions()
                    self.register(index, existing[0]['id'] if existing else None)
                    order.append(index)
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertFalse(errors)
        with self.pool.hold('recovery', 'test'):
            self.assertEqual(len(self.pool.pending_questions()), 1)
            self.assertEqual(len(self.pool.pending_questions()[0]['links']), 2)

    def test_lock_timeout_and_expired_owner_fencing(self):
        other = SharedQuestionPool(self.store)
        with self.pool.hold('worker', 'apply-1', lease_seconds=.05) as old_token:
            time.sleep(.06)
            with other.hold('worker', 'apply-2'):
                with self.assertRaises(ReviewError):
                    self.pool.check(old_token)
                with self.assertRaises(ReviewError):
                    self.pool.renew(old_token)
                with self.assertRaises(ReviewError):
                    self.register()
            self.assertEqual(other.lock_snapshot()['state'], 'free')

    def test_lease_can_be_renewed_from_heartbeat_thread(self):
        errors = []
        with self.pool.hold('worker', 'apply-1', lease_seconds=.1) as token:
            def heartbeat():
                try: self.pool.renew(token, lease_seconds=1)
                except Exception as error: errors.append(error)
            thread = threading.Thread(target=heartbeat)
            thread.start(); thread.join()
            time.sleep(.11)
            self.pool.check(token)
        self.assertFalse(errors)

    def test_scope_cannot_be_widened_by_worker(self):
        pid, _, qid = self.processes[0]
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET text='Accept employer privacy notice?' WHERE id=?", (qid,))
        with self.pool.hold('worker', 'apply-1'):
            with self.assertRaises(ReviewError):
                self.pool.register_questions(pid, 'apply-1', [qid], scopes={qid: {'kind': 'personal', 'key': ''}})

    def test_uncertain_submission_not_propagated(self):
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            self.register(1, pool_id)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE id=?", (self.processes[1][0],))
        result = self.save()
        self.assertEqual(result['answer_requests'], [])

    def test_answer_fanout_preserves_independent_browser_gate(self):
        pid, task_id, _ = self.processes[1]
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            self.register(1, pool_id)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (pid,))
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?", (task_id,))
            self.store._process_event(db, pid, 'blocked',
                {'reason': 'tool_approval_required', 'operation': 'resume_upload'}, 'worker')
        self.assertEqual(len(self.save()['answer_requests']), 1)
        with self.pool.hold('recovery', 'test'):
            self.assertEqual(self.pool.propagate_answers()['propagated_process_ids'], [pid])
            self.assertEqual(self.pool.linked_answer_requests(), [])
        with self.store.transaction() as db:
            self.assertEqual(json.loads(db.execute("SELECT answer FROM wf_questions WHERE id='q1'").fetchone()[0]), 'yes-1')
            self.assertEqual(self.store._task(db, task_id)['status'], 'waiting_for_user')
            self.assertEqual(self.store._task(db, task_id)['error'], 'tool_approval_required')
            self.assertEqual(self.store._process(db, pid)['block_reason'], 'tool_approval_required')

    def test_browser_gate_requires_original_authority_event(self):
        pid, task_id, _ = self.processes[1]
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            self.register(1, pool_id)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (pid,))
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?", (task_id,))
        self.assertEqual(self.save()['answer_requests'], [])

    def test_only_verified_not_started_attempt_allows_fanout(self):
        pid, task_id, _ = self.processes[1]
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            self.register(1, pool_id)
        with self.store.transaction(True) as db:
            db.execute('INSERT INTO wf_submission_attempts(id,process_id,task_id,form_version,started_at) VALUES(?,?,?,?,?)',
                       ('old-attempt', pid, task_id, 'form-1', now()))
        self.assertEqual(self.save()['answer_requests'], [])
        with self.store.transaction(True) as db:
            checkpoint = json.loads(self.store._task(db, task_id)['checkpoint'])
            checkpoint['reconciled_submission_attempt_id'] = 'old-attempt'
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?', (json.dumps(checkpoint), task_id))
        with self.pool.hold('recovery', 'test'):
            self.assertEqual(self.pool.linked_answer_requests(), [])
            with self.store.transaction(True) as db:
                self.store._process_event(db, pid, 'submission_attempt_reconciled', {
                    'attempt_id': 'old-attempt', 'outcome': 'not_started', 'external_item_count': 0,
                    'submission_replayed': False, 'fresh_preparation_required': True}, 'operator')
            self.assertEqual(self.pool.propagate_answers()['propagated_process_ids'], [pid])
        with self.store.transaction() as db:
            self.assertEqual(self.store._task(db, task_id)['status'], 'queued')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0], 1)

    def test_revoked_target_does_not_stop_independent_fanout_and_is_audited(self):
        self.add_application('a:3')
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            self.register(1, pool_id)
            self.register(2, pool_id)
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='later' WHERE key='a:2'")
        result = self.save()
        self.assertEqual([r['payload']['process_id'] for r in result['answer_requests']], [self.processes[2][0]])
        with self.pool.hold('recovery', 'test'):
            self.assertEqual(self.pool.propagate_answers()['propagated_process_ids'], [self.processes[2][0]])
            self.pool.linked_answer_requests()
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT answer FROM wf_questions WHERE id='q1'").fetchone()[0])
            self.assertEqual(self.store._task(db, self.processes[1][1])['status'], 'waiting_for_user')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wf_process_events WHERE type='shared_answer_deferred'").fetchone()[0], 1)
        with self.assertRaises(ReviewError):
            self.store._answer_questions(self.payload(1, request='revoked-direct'))

    def test_propagated_copy_never_becomes_a_new_user_source(self):
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            self.register(1, pool_id)
        self.save()
        with self.pool.hold('recovery', 'test'):
            self.pool.propagate_answers()
            self.pool.propagate_answers()
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_profile_answers').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0], 1)
            source = json.loads(db.execute('SELECT record FROM wf_profile_answers').fetchone()[0])
            self.assertEqual(source['question_id'], 'q0')
            self.assertEqual(source['reuse_scope'], {'kind': 'personal', 'key': ''})

    def test_manifest_crash_is_repaired_from_database(self):
        self.save()
        manifest = self.pool.directory / 'answered-questions.current.json'
        manifest.write_text('{broken')
        with self.pool.hold('recovery', 'startup'):
            snapshot = self.pool.answer_snapshot()
            self.assertIn('Yes', snapshot['markdown'])
            self.assertEqual(snapshot['manifest']['algorithm'], 'blake3')

    def test_matching_stale_answer_acknowledged_conflicting_answer_rejected(self):
        stale = self.payload(request='other-client')
        self.save()
        same = self.pool.formatting_spec(stale)
        self.assertTrue(same['already_answered'])
        self.assertEqual(same['receipt']['process_ids'], [self.processes[0][0]])
        conflict = copy.deepcopy(stale)
        conflict['answers'][0]['value'] = 'no-0'
        with self.assertRaises(ReviewError):
            self.pool.formatting_spec(conflict)
        with self.store.transaction() as db:
            self.assertEqual(json.loads(db.execute("SELECT answer FROM wf_questions WHERE id='q0'").fetchone()[0]), 'yes-0')


    def test_answer_formatted_before_another_client_saved_gets_safe_ack(self):
        first = self.pool.formatting_spec(self.payload(request='client-a'))
        second = self.pool.formatting_spec(self.payload(request='client-b'))
        self.save(first)
        result = self.save(second)
        self.assertTrue(result['already_answered'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0], 1)

    def test_scope_narrowed_answer_does_not_resolve_other_application(self):
        with self.pool.hold('worker', 'apply-1'):
            pool_id = self.register()['mappings']['q0']
            self.register(1, pool_id)
        payload = self.payload()
        payload['pool_scopes'] = {'q0': {'kind': 'application', 'key': self.processes[0][0]}}
        result = self.save(self.pool.formatting_spec(payload))
        self.assertEqual(result['answer_requests'], [])
        with self.store.transaction() as db:
            record = json.loads(db.execute('SELECT record FROM wf_profile_answers').fetchone()[0])
            self.assertEqual(record['reuse_scope'], payload['pool_scopes']['q0'])
        with self.pool.hold('worker', 'apply-2'):
            self.assertEqual(len(self.pool.pending_questions()), 1)
            self.assertEqual(self.pool.pending_questions()[0]['links'][0]['question_id'], 'q1')

    def test_crash_after_user_commit_before_projection_recovers_exact_append(self):
        spec = self.pool.formatting_spec(self.payload())
        with self.pool.hold('answer_handler', 'user'):
            with patch.object(self.pool, '_publish', side_effect=OSError('fictional disk failure')):
                with self.assertRaises(OSError):
                    self.pool.save_answered_questions(spec['append_text'])
        recovered = SharedQuestionPool(self.store)
        with recovered.hold('recovery', 'startup'):
            snapshot = recovered.answer_snapshot()
            self.assertIn(spec['append_text'], snapshot['markdown'])
            self.assertEqual(recovered.snapshot()['question_pool']['answered_count'], 1)

    def test_crash_before_pool_record_restores_narrowed_profile_scope(self):
        payload = self.payload()
        scope = {'kind': 'application', 'key': self.processes[0][0]}
        payload['pool_scopes'] = {'q0': scope}
        spec = self.pool.formatting_spec(payload)
        with self.pool.hold('answer_handler', 'user'):
            with patch.object(self.pool, '_record_answer', side_effect=OSError('fictional interruption')):
                with self.assertRaises(OSError):
                    self.pool.save_answered_questions(spec['append_text'])
        recovered = SharedQuestionPool(self.store)
        with recovered.hold('recovery', 'startup'):
            snapshot = recovered.answer_snapshot()
            self.assertEqual(snapshot['records'][0]['scope'], scope)
            with self.store.transaction() as db:
                record = json.loads(db.execute('SELECT record FROM wf_profile_answers').fetchone()[0])
                self.assertEqual(record['reuse_scope'], scope)

    def test_seed_preserves_explicit_scope_and_history(self):
        self.store._answer_questions(self.payload())
        records = self.store.profile_answer_records()
        # API returns authoritative records with user answer chronology.
        if isinstance(records, dict):
            records = records['records']
        with self.pool.hold('recovery', 'seed'):
            self.pool.seed_user_records(records)
            snapshot = self.pool.answer_snapshot()
            self.assertEqual(snapshot['records'][0]['scope']['kind'], 'application')
            manifest = snapshot['manifest']
            self.pool.seed_user_records(records)
            self.assertEqual(self.pool.answer_snapshot()['manifest'], manifest)



    def test_same_request_id_different_body_rejected_even_if_value_matches(self):
        original = self.payload(request='stable-request')
        self.save(self.pool.formatting_spec(original))
        replay = self.pool.formatting_spec(original)
        self.assertTrue(replay['replayed'])
        conflicting = self.payload(request='stable-request')
        with self.assertRaises(ReviewError) as caught:
            self.pool.formatting_spec(conflicting)
        self.assertEqual(caught.exception.reason, 'request_id_conflict')

    def test_preformatted_conflicting_request_id_is_rejected_under_lock(self):
        first = self.pool.formatting_spec(self.payload(request='shared-request'))
        second_payload = self.payload(request='shared-request')
        second_payload['answers'][0]['value'] = 'no-0'
        second = self.pool.formatting_spec(second_payload)
        self.save(first)
        with self.assertRaises(ReviewError) as caught:
            self.save(second)
        self.assertEqual(caught.exception.reason, 'request_id_conflict')

    def external_record(self, value='Yes', revision=1):
        source = self.root / f'confirmed-{revision}.md'
        source.write_text(f'User confirmed: {value}\n')
        return {'external_id': 'profile.work-authorization', 'revision': revision,
            'authority': 'user', 'question_text': 'Are you authorized to work in the United States?',
            'type': 'single_choice', 'choices': [{'id': 'yes', 'label': 'Yes'}, {'id': 'no', 'label': 'No'}],
            'value': value.lower(), 'reuse_scope': {'kind': 'personal', 'key': ''},
            'answered_at': f'2026-10-23T20:00:0{revision}+00:00',
            'source': {'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                       'start_line': 1, 'end_line': 1, 'quote': f'User confirmed: {value}'}}

    def test_external_seed_verifies_source_and_preserves_corrections(self):
        first = self.external_record()
        second = self.external_record('No', 2)
        with self.pool.hold('recovery', 'seed'):
            result = self.pool.seed_external_user_records([first])
            self.assertEqual(result['seeded'], 1)
            duplicate = self.pool.seed_external_user_records([first])
            self.assertEqual(duplicate['seeded'], 0)
            self.pool.seed_external_user_records([second])
            snap = self.pool.answer_snapshot()
            self.assertEqual(len(snap['records']), 2)
            self.assertEqual([r['value'] for r in snap['records']], ['yes', 'no'])
            pool_id = snap['records'][-1]['pool_id']
            result = self.register(1, pool_id)
            self.assertEqual(result['answer_requests'][0]['payload']['answers'][0]['value'], 'no-1')
            with self.assertRaises(ReviewError):
                self.pool.seed_external_user_records([first])

    def test_external_seed_rejects_changed_source_or_invented_scope(self):
        record = self.external_record()
        with self.pool.hold('recovery', 'seed'):
            bad = copy.deepcopy(record)
            bad['source']['quote'] = 'User confirmed: No'
            with self.assertRaises(ReviewError): self.pool.seed_external_user_records([bad])
            bad = copy.deepcopy(record)
            del bad['reuse_scope']
            with self.assertRaises(ReviewError): self.pool.seed_external_user_records([bad])
            Path(record['source']['path']).write_text('Different bytes\n')
            with self.assertRaises(ReviewError): self.pool.seed_external_user_records([record])
            self.assertEqual(self.pool.snapshot()['question_pool']['answered_count'], 0)

    def test_answer_api_rejects_lease_expiry_inside_write_transaction(self):
        payload = self.payload()
        original = self.store._validate_answer
        def delayed(question, value):
            time.sleep(.06)
            return original(question, value)
        with self.pool.hold('answer_handler', 'user', lease_seconds=.04):
            with patch.object(self.store, '_validate_answer', side_effect=delayed):
                with self.assertRaises(ReviewError): self.pool._apply_answer_payload(payload)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT answer FROM wf_questions WHERE id='q0'").fetchone()[0])

    def test_diagnostic_snapshot_has_no_question_text_or_paths(self):
        with self.pool.hold('worker', 'apply-1'):
            self.register()
            snapshot = self.pool.snapshot()
            self.assertEqual(snapshot['question_lock']['owner_id'], 'apply-1')
            self.assertNotIn(str(self.root), json.dumps(snapshot))
            self.assertNotIn('authorized', json.dumps(snapshot))
            enriched = self.pool.question_enrichment(['q0', 'q1'])
            self.assertIsNotNone(enriched['q0']['pool_question_id'])
            self.assertIsNone(enriched['q1']['pool_question_id'])


if __name__ == '__main__':
    unittest.main()
