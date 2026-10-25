"""Fictional cutover tests. No native worker, browser, or production calls."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from application_pool_cutover import apply_plan, build_plan
from question_pool import SharedQuestionPool
from store import ReviewError, encode, now
from test_store import catalog
from workflow import WorkflowStore


class ApplicationPoolCutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WorkflowStore(self.root / 'review.sqlite3')
        self.store.import_catalog(catalog(), 'fictional')
        self.pool = SharedQuestionPool(self.store).initialize()
        self.store.question_pool = self.pool
        self.apps = []
        for number, key in enumerate(('a:1', 'a:2', 'a:3', 'b:b1')):
            with self.store.transaction(True) as db:
                db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key=?", (key,))
                pid = self.store._create_application(db, self.store._row(db, 'jobs', 'key', key), f'approve-{number}')
            task = self.store.lease_task(f'fixture-{number}', ['apply'])
            self.store.start_application(pid, task['lease_token'])
            qid = f'q{number}'
            self.store.checkpoint_task(task['id'], task['lease_token'], {'question_bindings': {qid: 'authorization'}, 'old_form': True})
            self.store.ask_questions(pid, task['lease_token'], f'form-{number}', [
                {'id': qid, 'text': 'May this employer contact your current company?', 'type': 'single_choice',
                 'required': True, 'choices': [{'id': 'yes', 'label': 'Yes'}, {'id': 'no', 'label': 'No'}]},
                {'id': qid + '-pending', 'text': 'An unanswered required fact?', 'type': 'text', 'required': True}])
            self.apps.append((pid, task['id'], qid))
        # A real accepted answer request and immutable user record, not a model claim.
        pid, _, qid = self.apps[0]
        with self.store.transaction() as db:
            process = self.store._process(db, pid)
        self.store._answer_questions({'request_id': 'confirmed-answer', 'process_id': pid,
            'process_revision': process['revision'], 'form_version': process['form_version'],
            'answers': [{'question_id': qid, 'question_revision': 1, 'value': 'yes'}]})
        with self.pool.hold('recovery', 'fictional-setup'):
            for index, (pid, _, qid) in enumerate(self.apps):
                self.pool.register_questions(pid, f'apply-{index % 3 + 1}', [qid + '-pending'])
        # Unknown and confirmed attempts are excluded regardless of an old task state.
        with self.store.transaction(True) as db:
            for index, status in ((1, 'submission_unknown'), (2, 'submitted')):
                pid, tid, _ = self.apps[index]
                db.execute('UPDATE wf_processes SET submission_status=?,receipt=? WHERE id=?',
                    (status, encode({'status': status, 'fictional': True}), pid))
                db.execute('INSERT INTO wf_submission_attempts VALUES(?,?,?,?,?,?,?)',
                    (f'attempt-{index}', pid, tid, f'form-{index}', now(), now() if index == 2 else None,
                     encode({'confirmation': 'fictional'}) if index == 2 else None))
            pid, tid, _ = self.apps[0]
            db.execute('UPDATE wf_tasks SET failure_count=max_attempts,attempts=8 WHERE id=?', (tid,))
        self.profile = self.root / 'profile.md'
        self.profile.write_text('Fictional source only.\nThe user accepted the Beta privacy notice for application 4.\n')
        self.evidence = self.root / 'stopped-owners.json'
        self.evidence.write_text('{"fixture": true, "owners_stopped": true, "owned_tabs_closed": true}\n')
        self.external = [{'authority': 'user', 'external_id': 'beta-privacy', 'revision': 1,
            'question_text': 'Accept the Beta privacy notice for application 4?', 'type': 'single_choice',
            'choices': [{'id': 'yes', 'label': 'Accepted'}, {'id': 'no', 'label': 'Not accepted'}],
            'value': 'yes', 'answered_at': '2026-10-23T12:00:00+00:00',
            'reuse_scope': {'kind': 'application', 'key': self.apps[3][0]},
            'source': {'path': str(self.profile), 'sha256': hashlib.sha256(self.profile.read_bytes()).hexdigest(),
                'start_line': 2, 'end_line': 2, 'quote': self.profile.read_text().splitlines()[1]}}]

    def plan(self):
        return build_plan(self.store.path, profile=self.profile, external_records=self.external,
                          evidence_files=[self.evidence])

    def proof(self):
        return {'dispatch_stopped': True, 'native_workers_stopped': True,
            'evidence_reference': str(self.evidence), 'task_session_resets': {
                tid: {'task_id': tid, 'worker_id': f'apply-{index % 3 + 1}',
                      'legacy_thread_id': '00000000-0000-4000-8000-000000000001',
                      'safe_preparation_reset': True, 'stopped_worker_confirmed': True,
                      'owned_tabs_closed': True, 'evidence_reference': str(self.evidence)}
                for index, (_, tid, _) in enumerate(self.apps)}}

    def apply(self, plan=None, **kwargs):
        defaults = {'backup_directory': self.root / 'backup', 'stop_evidence': self.proof(),
                    'selected_process_ids': [self.apps[0][0], self.apps[3][0]]}
        defaults.update(kwargs)
        return apply_plan(self.store, plan or self.plan(), **defaults)

    def rows(self, table):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')]

    def test_read_only_plan_classifies_outcomes_and_uses_started_at(self):
        before = self.rows('wf_tasks')
        plan = self.plan()
        kinds = {item['process_id']: item['classification'] for item in plan['items']}
        self.assertEqual([kinds[pid] for pid, _, _ in self.apps], [
            'fresh_preparation_candidate', 'reconciliation_required', 'submitted_excluded',
            'fresh_preparation_candidate'])
        self.assertEqual(before, self.rows('wf_tasks'))
        self.assertEqual(plan['plan_hash'], self.plan()['plan_hash'])
        self.assertEqual(len(plan['user_answer_records']), 1)
        self.assertFalse((self.root / 'backup').exists())

    def test_backed_up_reset_retains_ids_history_scope_and_empty_pending(self):
        before_tasks = {row['id']: row for row in self.rows('wf_tasks')}
        before_processes = {row['id']: row for row in self.rows('wf_processes')}
        attempts = self.rows('wf_submission_attempts')
        requests = self.rows('wf_answer_requests')
        profile_rows = self.rows('wf_profile_answers')
        pending_file = (self.pool.directory / 'pending-questions.md').read_bytes()
        result = self.apply()
        self.assertEqual((result['pending_count'], result['integrity'], result['foreign_key_errors']), (0, 'ok', 0))
        self.assertEqual(attempts, self.rows('wf_submission_attempts'))
        self.assertEqual(requests, self.rows('wf_answer_requests'))
        expected_profile_rows = copy.deepcopy(profile_rows)
        for row in expected_profile_rows:
            record = json.loads(row['record'])
            record['reuse_scope'] = {'kind': 'application', 'key': record['process_id']}
            row['record'] = encode(record)
        # Only the validated narrow reuse scope is additive. Keep every saved
        # value, identity, revision, source field, timestamp and row unchanged.
        self.assertEqual(expected_profile_rows, self.rows('wf_profile_answers'))
        self.assertEqual(self.profile.read_bytes(), next((self.root / 'backup').glob('source-*-profile.md')).read_bytes())
        self.assertEqual(pending_file, (self.root / 'backup/question-pool/pending-questions.md').read_bytes())
        self.assertEqual((self.root / 'backup/before.sqlite3').stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.root / 'backup/before.sqlite3') as saved:
            self.assertEqual(saved.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(saved.execute('SELECT failure_count FROM wf_tasks WHERE id=?', (self.apps[0][1],)).fetchone()[0], 3)
        with self.store.transaction() as db:
            for index in (1, 2):
                pid, tid, _ = self.apps[index]
                self.assertEqual(before_tasks[tid], dict(db.execute('SELECT * FROM wf_tasks WHERE id=?', (tid,)).fetchone()))
                self.assertEqual(before_processes[pid], dict(db.execute('SELECT * FROM wf_processes WHERE id=?', (pid,)).fetchone()))
            pid, tid, qid = self.apps[0]
            task = dict(db.execute('SELECT * FROM wf_tasks WHERE id=?', (tid,)).fetchone())
            checkpoint = json.loads(task['checkpoint'])
            self.assertEqual((task['status'], task['failure_count'], task['attempts']), ('queued', 0, 8))
            self.assertNotIn('question_bindings', checkpoint)
            self.assertEqual(checkpoint['preparation_reset']['previous_checkpoint']['question_bindings'], {qid: 'authorization'})
            self.assertIsNone(db.execute('SELECT form_version FROM wf_processes WHERE id=?', (pid,)).fetchone()[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_questions WHERE superseded=0').fetchone()[0], 0)
            self.assertEqual(json.loads(db.execute('SELECT answer FROM wf_questions WHERE id=?', (qid,)).fetchone()[0]), 'yes')
            scopes = {(r['scope_kind'], r['scope_key']) for r in db.execute('SELECT * FROM wf_qpool_questions WHERE id IN (SELECT pool_id FROM wf_qpool_answers)')}
            self.assertEqual(scopes, {('application', self.apps[0][0]), ('application', self.apps[3][0])})
            sources = [json.loads(r[0]) for r in db.execute('SELECT source FROM wf_qpool_answers')]
            self.assertEqual(len(sources), 2)
        with self.pool.hold('recovery', 'check'):
            answer = self.pool.answer_snapshot()
        self.assertIn('Accepted', answer['markdown'])
        self.assertEqual(answer['manifest']['algorithm'], 'blake3')
        import blake3
        self.assertEqual(answer['manifest']['digest'], blake3.blake3(answer['markdown'].encode()).hexdigest())
        # A new form round succeeds without preserving old answered field IDs.
        lease = self.store.lease_task('new-worker', ['apply'], application_worker_id='apply-1')
        self.assertEqual(lease['id'], self.apps[0][1])
        self.store.start_application(self.apps[0][0], lease['lease_token'])
        self.store.ask_questions(self.apps[0][0], lease['lease_token'], 'new-form', [
            {'id': 'new-question', 'text': 'A new required fact?', 'type': 'text', 'required': True}])

    def test_unknown_selection_rejected_before_backup(self):
        with self.assertRaises(ReviewError):
            self.apply(selected_process_ids=[self.apps[1][0]])
        self.assertFalse((self.root / 'backup').exists())

    def failed_plan(self):
        pid,tid,_=self.apps[0]
        with self.store.transaction(True) as db:
            db.execute("UPDATE queue SET status='failed' WHERE id=?",(pid,))
            db.execute("UPDATE wf_tasks SET status='failed',error='task_failed' WHERE id=?",(tid,))
        authority={'version':1,'source':'user_chat','source_reference':str(self.evidence),
            'process_ids':[pid],'allow_failed_preparation_reset':True}
        return build_plan(self.store.path,evidence_files=[self.evidence],
                          fresh_preparation_authority=authority)

    def test_failed_application_requires_explicit_fresh_authority_and_consent(self):
        plan=self.failed_plan()
        self.assertEqual('authorization_required',next(i['classification'] for i in plan['items']
                         if i['process_id']==self.apps[0][0]))
        self.store.record_existing_project_consent(request_id='fictional-consent',source_reference='fictional-user')
        self.assertEqual('authorization_required',next(i['classification'] for i in self.plan()['items']
                         if i['process_id']==self.apps[0][0]))
        with self.assertRaises(ReviewError):
            self.apply(selected_process_ids=[self.apps[0][0]])
        plan=self.failed_plan()
        self.apply(plan,selected_process_ids=[self.apps[0][0]])
        with self.store.transaction() as db:
            task=self.store._task(db,self.apps[0][1])
            self.assertEqual(('queued',0,8),(task['status'],task['failure_count'],task['attempts']))
            event=json.loads(db.execute("SELECT data FROM wf_process_events WHERE process_id=? AND type='application_pool_preparation_reset'",
                                      (self.apps[0][0],)).fetchone()[0])
            self.assertEqual('failed',event['previous']['queue']['status'])
            self.assertEqual(3,event['previous']['task']['failure_count'])
            self.assertEqual(plan['fresh_preparation_authority'],event['fresh_preparation_authority'])

    def test_fresh_authority_preserves_tool_blocker_and_is_consent_fenced(self):
        self.store.record_existing_project_consent(request_id='fictional-consent',source_reference='fictional-user')
        plan=self.failed_plan()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?",(self.apps[0][1],))
        with self.assertRaises(ReviewError):
            self.apply(plan,selected_process_ids=[self.apps[0][0]])
        held=build_plan(self.store.path,fresh_preparation_authority=plan['fresh_preparation_authority'])
        self.assertEqual('independent_blocker',next(i['classification'] for i in held['items']
                         if i['process_id']==self.apps[0][0]))
        plan=self.failed_plan()
        with self.store.transaction(True) as db:
            db.execute('DELETE FROM wf_project_consent')
        with self.assertRaises(ReviewError):
            self.apply(plan,selected_process_ids=[self.apps[0][0]])

    def test_stale_plan_rejects_content_change_even_when_timestamps_unchanged(self):
        plan = self.plan()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET checkpoint='{}' WHERE id=?", (self.apps[0][1],))
        with self.assertRaisesRegex(ReviewError, 'state or source evidence changed'):
            self.apply(plan)
        self.assertFalse((self.root / 'backup').exists())

    def test_stale_plan_rejects_authorization_and_question_changes(self):
        for sql, args in [("UPDATE jobs SET decision='reject' WHERE key=?", ('a:1',)),
                          ('UPDATE wf_questions SET text=? WHERE id=?', ('Changed fact?', 'q0'))]:
            plan = self.plan()
            with self.store.transaction(True) as db:
                db.execute(sql, args)
            with self.assertRaises(ReviewError):
                self.apply(plan)
        self.assertFalse((self.root / 'backup').exists())

    def test_stale_source_and_changed_seed_are_rejected(self):
        plan = self.plan()
        self.evidence.write_text('Changed evidence')
        with self.assertRaises(ReviewError):
            self.apply(plan)
        plan = self.plan()
        widened = copy.deepcopy(plan['user_answer_records'])
        widened[0]['reuse_scope'] = {'kind': 'personal', 'key': ''}
        with self.assertRaisesRegex(ReviewError, 'seeds must match'):
            self.apply(plan, seeded_records=widened)

    def test_proof_requires_all_selected_task_owners_and_owned_tabs(self):
        for field in ('dispatch_stopped', 'native_workers_stopped'):
            proof = self.proof()
            proof[field] = False
            with self.assertRaises(ReviewError):
                self.apply(stop_evidence=proof)
        for field, value in [('task_id', 'wrong'), ('worker_id', 'apply-4'),
                             ('owned_tabs_closed', False), ('stopped_worker_confirmed', False)]:
            proof = self.proof()
            proof['task_session_resets'][self.apps[0][1]][field] = value
            with self.assertRaises(ReviewError):
                self.apply(stop_evidence=proof)
        self.assertFalse((self.root / 'backup').exists())

    def test_pending_pool_archival_does_not_clear_independent_browser_blocker(self):
        pid, tid, _ = self.apps[3]
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (pid,))
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?", (tid,))
        plan = self.plan()
        self.assertEqual(next(i for i in plan['items'] if i['process_id'] == pid)['classification'], 'independent_blocker')
        with self.assertRaises(ReviewError):
            self.apply(plan)
        before_tasks = self.rows('wf_tasks')
        result = self.apply(plan, selected_process_ids=[self.apps[0][0]])
        self.assertEqual(result['pending_count'], 0)
        self.assertEqual(next(t for t in before_tasks if t['id'] == tid), next(t for t in self.rows('wf_tasks') if t['id'] == tid))

    def test_other_running_owner_blocks_cutover(self):
        with self.store.transaction(True) as db:
            self.store._enqueue_task(db, 'explore', {}, dedupe_key='fictional-discovery', resource='browser')
        lease = self.store.lease_task('discovery', ['explore'])
        self.assertIsNotNone(lease)
        before = self.rows('wf_tasks')
        with self.assertRaisesRegex(ReviewError, 'old resource or task owner remains'):
            self.apply()
        self.assertEqual(before, self.rows('wf_tasks'))
        self.assertFalse((self.root / 'backup').exists())

    def test_writer_during_backup_is_rejected_before_reset(self):
        plan = self.plan()
        import application_pool_cutover as cutover
        original = cutover._backup
        def change_after_backup(*args):
            original(*args)
            with self.store.transaction(True) as db:
                db.execute('UPDATE wf_tasks SET failure_count=2 WHERE id=?', (self.apps[0][1],))
        with patch.object(cutover, '_backup', side_effect=change_after_backup):
            with self.assertRaises(ReviewError):
                self.apply(plan)
        self.assertTrue((self.root / 'backup/before.sqlite3').exists())
        self.assertFalse((self.root / 'backup/result.json').exists())
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT form_version FROM wf_processes WHERE id=?', (self.apps[0][0],)).fetchone()[0], 'form-0')

    def test_cli_dry_run_does_not_mutate_database(self):
        output = self.root / 'plan.json'
        before = self.rows('wf_tasks')
        completed = subprocess.run([sys.executable, '-B', 'application_pool_cutover.py',
            '--database', str(self.store.path), '--profile', str(self.profile),
            '--evidence-file', str(self.evidence), '--output', str(output)],
            cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
        self.assertIn('fresh_preparation_candidate', completed.stdout)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, self.rows('wf_tasks'))
        self.assertFalse((self.root / 'backup').exists())

    def test_sqlite_backup_captures_committed_wal_pages(self):
        # Keep a reader open so the committed record remains in the live WAL.
        connection = self.store.connect()
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('BEGIN')
        connection.execute('SELECT * FROM wf_settings').fetchall()
        with self.store.transaction(True) as db:
            db.execute("INSERT INTO wf_settings VALUES('fictional_wal_marker','retained')")
        self.assertTrue(Path(str(self.store.path) + '-wal').exists())
        self.apply()
        with sqlite3.connect(self.root / 'backup/before.sqlite3') as saved:
            self.assertEqual(saved.execute("SELECT value FROM wf_settings WHERE key='fictional_wal_marker'").fetchone()[0], 'retained')

    def test_writer_during_seed_preserves_reset_fence_and_accepted_seed(self):
        plan = self.plan()
        original = self.pool.seed_user_records
        def concurrent_writer(records):
            result = original(records)
            with self.store.transaction(True) as db:
                db.execute('UPDATE wf_tasks SET failure_count=2 WHERE id=?', (self.apps[0][1],))
            return result
        with patch.object(self.pool, 'seed_user_records', side_effect=concurrent_writer):
            with self.assertRaisesRegex(ReviewError, 'No tasks were reset'):
                self.apply(plan)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT form_version FROM wf_processes WHERE id=?', (self.apps[0][0],)).fetchone()[0], 'form-0')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT superseded FROM wf_questions WHERE id=?', (self.apps[0][2],)).fetchone()[0], 0)

    def test_not_started_candidate_enters_fresh_preparation(self):
        pid, tid, _ = self.apps[3]
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='not_started' WHERE id=?", (pid,))
        self.apply(selected_process_ids=[pid])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT submission_status FROM wf_processes WHERE id=?', (pid,)).fetchone()[0], 'preparing')
            self.assertEqual(db.execute('SELECT status FROM wf_tasks WHERE id=?', (tid,)).fetchone()[0], 'queued')

    def test_plan_content_tampering_is_rejected(self):
        plan = self.plan()
        plan['items'][0]['original_block_reason'] = 'fabricated'
        with self.assertRaisesRegex(ReviewError, 'plan was changed after review'):
            self.apply(plan)
        self.assertFalse((self.root / 'backup').exists())


if __name__ == '__main__':
    unittest.main()
