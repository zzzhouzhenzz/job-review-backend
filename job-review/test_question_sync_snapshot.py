"""Fictional Mac/iOS clients read one canonical, transactional question snapshot.

No native process, browser, production database, or external endpoint is used.
The existing fixture is composed by module alias so its tests are not inherited.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import unittest

import test_question_sync_api as fixtures


class QuestionSyncSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.QuestionSyncAPITests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    @property
    def store(self):
        return self.fixture.store

    @property
    def pool(self):
        return self.fixture.pool

    def request(self, method, path, payload=None, device='Mac'):
        return self.fixture.request(method, path, payload, device)

    def snapshot(self, device='Mac', state='ready'):
        status, body = self.request('GET', '/api/v1/question-sync', device=device)
        self.assertEqual(status, 200, body)
        self.assertTrue(body['complete'])
        snapshot = body['question_sync']
        self.assertEqual(snapshot['sync']['state'], state, body)
        self.assertIs(type(snapshot['sync']['revision']), int)
        self.assertIs(type(snapshot['sync']['pool_revision']), int)
        self.assertGreaterEqual(snapshot['sync']['revision'], 0)
        self.assertGreaterEqual(snapshot['sync']['pool_revision'], 0)
        if state == 'ready':
            for key in ('shared_questions', 'questions', 'processes'):
                self.assertIsInstance(snapshot[key], list)
        else:
            for key in ('shared_questions', 'questions', 'processes'):
                self.assertIsNone(snapshot[key], key)
        return snapshot

    def command(self, snapshot, index=0, device='Mac', value=None, question_id=None, request_id=None):
        qid = question_id or f'q{index}'
        question = next(q for q in snapshot['questions'] if q['id'] == qid)
        process = next(p for p in snapshot['processes'] if p['id'] == question['process_id'])
        return {'request_id': request_id or f'{device}-{qid}', 'process_id': process['id'],
                'process_revision': process['revision'], 'form_version': question['form_version'],
                'expected_pool_revision': snapshot['sync']['pool_revision'],
                'answers': [{'question_id': qid, 'question_revision': question['revision'],
                             'value': f'yes-{index}' if value is None else value}]}

    def answer_count(self):
        with self.store.transaction() as db:
            return db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0]

    def assert_saved(self, snapshot, *, expected='Yes'):
        self.assertEqual(len(snapshot['shared_questions']), 1)
        shared = snapshot['shared_questions'][0]
        self.assertEqual(shared['id'], self.fixture.shared)
        self.assertEqual(shared['status'], 'answered')
        self.assertEqual(shared['scope'], {'kind': 'personal', 'key': ''})
        self.assertEqual(shared['type'], 'single_choice')
        self.assertTrue(shared['required'])
        self.assertEqual(set(shared['binding_ids']), {'q0', 'q1'})
        choices = {choice['label']: choice['id'] for choice in shared['choices']}
        self.assertEqual(shared['answer'], choices[expected])
        self.assertTrue(shared['answered_at'])
        self.assertGreater(shared['answer_revision'], 0)
        prefix = expected.lower()
        self.assertEqual({q['id']: q['answer'] for q in snapshot['questions']},
                         {'q0': f'{prefix}-0', 'q1': f'{prefix}-1'})
        for question in snapshot['questions']:
            self.assertEqual(question['pool_question_id'], shared['id'])
            self.assertEqual(question['pool_revision'], snapshot['sync']['pool_revision'])
            self.assertEqual(question['pool_answer_revision'], shared['answer_revision'])
            self.assertEqual(question['answered_at'], shared['answered_at'])
        self.assertEqual(self.answer_count(), 1)

    def test_capability_is_true_only_when_shared_pool_is_configured(self):
        status, body = self.request('GET', '/api/v1/capabilities')
        self.assertEqual(status, 200)
        self.assertIs(body['capabilities']['question_sync'], True)
        pool = self.store.question_pool
        try:
            self.store.question_pool = None
            status, body = self.request('GET', '/api/v1/capabilities')
            self.assertEqual(status, 200)
            self.assertIs(body['capabilities']['question_sync'], False)
        finally:
            self.store.question_pool = pool

    def test_initial_snapshot_has_one_canonical_question_and_both_form_bindings(self):
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot['shared_questions']), 1)
        question = snapshot['shared_questions'][0]
        self.assertEqual(question['id'], self.fixture.shared)
        self.assertEqual(question['text'], 'Are you authorized to work in the United States?')
        self.assertEqual(question['status'], 'open')
        self.assertIsNone(question['answer'])
        self.assertIsNone(question['answered_at'])
        self.assertEqual(question['answer_revision'], 0)
        self.assertEqual(set(question['binding_ids']), {'q0', 'q1'})
        self.assertEqual(len(snapshot['questions']), 2)
        self.assertEqual({p['id'] for p in snapshot['processes']}, {p[0] for p in self.fixture.processes})
        for form in snapshot['questions']:
            self.assertEqual(form['pool_question_id'], question['id'])
            self.assertEqual(form['pool_revision'], snapshot['sync']['pool_revision'])
            self.assertEqual(form['pool_answer_revision'], 0)
            self.assertIsNone(form['answer'])
            self.assertIsNone(form['answered_at'])

    def test_questions_and_processes_share_one_transaction_during_concurrent_write(self):
        initial = self.snapshot()
        pid = self.fixture.processes[0][0]
        entered, release = threading.Event(), threading.Event()
        original = self.store.question
        def paused(row):
            result = original(row)
            if row['id'] == 'q0' and not entered.is_set():
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Fictional test did not release the snapshot reader.')
            return result
        self.store.question = paused
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                read = executor.submit(self.snapshot, 'iOS')
                try:
                    self.assertTrue(entered.wait(2))
                    with self.pool.hold('worker', 'fictional-next-form'):
                        with self.store.transaction(True) as db:
                            db.execute("UPDATE wf_questions SET form_version='next-form',revision=revision+1 WHERE id='q0'")
                            db.execute("UPDATE wf_processes SET form_version='next-form',revision=revision+1 WHERE id=?", (pid,))
                            self.pool._bump(db)
                        self.pool.repair()
                finally:
                    release.set()
                during = read.result(timeout=3)
        finally:
            release.set()
            self.store.question = original
        for key, identity in (('questions', 'q0'), ('processes', pid)):
            old = next(item for item in initial[key] if item['id'] == identity)
            actual = next(item for item in during[key] if item['id'] == identity)
            self.assertEqual(actual['form_version'], old['form_version'])
            self.assertEqual(actual['revision'], old['revision'])
        current = self.snapshot('Mac')
        self.assertEqual(next(q for q in current['questions'] if q['id'] == 'q0')['form_version'], 'next-form')
        self.assertEqual(next(p for p in current['processes'] if p['id'] == pid)['form_version'], 'next-form')

    def test_mac_answer_appears_on_ios_once_with_local_option_ids(self):
        before = self.snapshot('Mac')
        status, receipt = self.request('POST', '/api/v1/answers', self.command(before), 'Mac')
        self.assertEqual(status, 200, receipt)
        after = self.snapshot('iOS')
        self.assert_saved(after)
        self.assertGreater(after['sync']['pool_revision'], before['sync']['pool_revision'])
        self.assertGreater(after['sync']['revision'], before['sync']['revision'])
        self.assertEqual(receipt['process_ids'], [self.fixture.processes[0][0]])
        self.assertEqual(receipt['task_ids'], [self.fixture.processes[0][1]])

    def test_ios_answer_appears_on_mac_using_canonical_choices(self):
        before = self.snapshot('iOS')
        status, receipt = self.request('POST', '/api/v1/answers', self.command(before, 1, 'iOS'), 'iOS')
        self.assertEqual(status, 200, receipt)
        after = self.snapshot('Mac')
        self.assert_saved(after)
        self.assertEqual(after['shared_questions'][0]['answer'], 'yes-0')

    def test_paused_writer_returns_early_updating_without_replacement_arrays(self):
        before = self.snapshot()
        command = self.command(before)
        entered, release = threading.Event(), threading.Event()
        original = self.store._answer_questions
        def paused(payload, **kwargs):
            if payload['process_id'] == self.fixture.processes[1][0]:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Fictional test did not release its paused writer.')
            return original(payload, **kwargs)
        self.store._answer_questions = paused
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                answer = executor.submit(self.request, 'POST', '/api/v1/answers', command)
                try:
                    self.assertTrue(entered.wait(2))
                    started = time.monotonic()
                    updating = self.snapshot('iOS', 'updating')
                    self.assertLess(time.monotonic() - started, .75)
                    self.assertFalse(answer.done())
                    self.assertGreaterEqual(updating['sync']['revision'], before['sync']['revision'])
                    # Null arrays are a status signal; the client keeps its ready data.
                    cached = copy.deepcopy(before)
                    if updating['questions'] is not None:
                        cached = updating
                    self.assertEqual(cached, before)
                    with self.store.transaction() as db:
                        process_states = [row[0] for row in db.execute('SELECT submission_status FROM wf_processes')]
                        task_states = [row[0] for row in db.execute('SELECT status FROM wf_tasks')]
                    self.assertNotIn('updating', process_states + task_states)
                    self.assertNotIn('pending', process_states + task_states)
                finally:
                    release.set()
                status, receipt = answer.result(timeout=3)
                self.assertEqual(status, 200, receipt)
        finally:
            release.set()
            self.store._answer_questions = original
        after = self.snapshot('iOS')
        self.assert_saved(after)
        self.assertGreaterEqual(after['sync']['revision'], updating['sync']['revision'])

    def test_missed_notification_reconnect_and_restart_preserve_monotonic_signal(self):
        before = self.snapshot('iOS')
        status, receipt = self.request('POST', '/api/v1/answers', self.command(before), 'Mac')
        self.assertEqual(status, 200, receipt)
        # The other client receives no notification, then refreshes on reconnect.
        after = self.snapshot('iOS')
        self.assert_saved(after)
        self.assertGreater(after['sync']['revision'], before['sync']['revision'])
        self.fixture.stop_server()
        self.fixture.start_server()
        restarted = self.snapshot('Mac')
        self.assert_saved(restarted)
        self.assertGreaterEqual(restarted['sync']['revision'], after['sync']['revision'])
        self.assertGreaterEqual(restarted['sync']['pool_revision'], after['sync']['pool_revision'])

    def test_active_worker_lock_returns_updating_without_waiting(self):
        before = self.snapshot()
        with self.pool.hold('worker', 'fixture-matching-worker'):
            started = time.monotonic()
            updating = self.snapshot('iOS', 'updating')
            self.assertLess(time.monotonic() - started, .75)
            self.assertGreaterEqual(updating['sync']['revision'], before['sync']['revision'])
        ready = self.snapshot('iOS')
        self.assertGreaterEqual(ready['sync']['revision'], updating['sync']['revision'])
        self.assertEqual(ready['shared_questions'][0]['status'], 'open')

    def test_missing_canonical_binding_reports_pending_with_null_arrays(self):
        with self.pool.hold('worker', 'fictional-interrupted-projection'):
            with self.store.transaction(True) as db:
                db.execute("DELETE FROM wf_qpool_links WHERE question_id='q1'")
                self.pool._bump(db)
        self.snapshot('Mac', 'pending')
        with self.store.transaction() as db:
            self.assertEqual({row[0] for row in db.execute('SELECT status FROM wf_tasks')},
                             {'waiting_for_user'})
            self.assertEqual({row[0] for row in db.execute('SELECT submission_status FROM wf_processes')},
                             {'preparing'})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_questions WHERE answer IS NULL').fetchone()[0], 2)

    def test_simultaneous_conflicting_stale_writes_accept_one_canonical_answer(self):
        initial = self.snapshot()
        commands = [self.command(initial, 0, 'Mac'), self.command(initial, 1, 'iOS', value='no-1')]
        barrier = threading.Barrier(2)
        def send(item):
            barrier.wait(2)
            return self.request('POST', '/api/v1/answers', item)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send, commands))
        self.assertEqual(sorted(status for status, _ in results), [200, 409], results)
        failure = next(body for status, body in results if status == 409)
        self.assertEqual(failure['error']['reason'], 'question_sync_stale')
        accepted = 0 if results[0][0] == 200 else 1
        self.assert_saved(self.snapshot('iOS'), expected='Yes' if accepted == 0 else 'No')

    def test_equivalent_stale_writes_and_exact_retry_do_not_append_answers(self):
        initial = self.snapshot()
        first = self.command(initial)
        equivalent = self.command(initial, 1, 'iOS')
        self.assertEqual(self.request('POST', '/api/v1/answers', first)[0], 200)
        calls = len(self.fixture.format_calls)
        for command in (equivalent, first, first):
            status, receipt = self.request('POST', '/api/v1/answers', command, 'iOS')
            self.assertEqual(status, 200, receipt)
            self.assertTrue(receipt['already_answered'])
        self.assertEqual(len(self.fixture.format_calls), calls)
        self.assert_saved(self.snapshot())
        conflict = copy.deepcopy(first)
        conflict['answers'][0]['value'] = 'no-0'
        self.assertEqual(self.request('POST', '/api/v1/answers', conflict)[0], 409)
        self.assertEqual(self.answer_count(), 1)

    def test_simultaneous_equivalent_answers_acknowledge_one_accepted_value(self):
        initial = self.snapshot()
        commands = [self.command(initial), self.command(initial, 1, 'iOS')]
        barrier = threading.Barrier(2)
        def send(command):
            barrier.wait(2)
            return self.request('POST', '/api/v1/answers', command)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send, commands))
        self.assertEqual([status for status, _ in results], [200, 200], results)
        self.assertTrue(any(receipt['already_answered'] for _, receipt in results))
        self.assert_saved(self.snapshot('iOS'))

    def add_unrelated_question(self):
        with self.store.transaction(True) as db:
            db.execute("""INSERT INTO wf_questions
                SELECT 'extra',process_id,form_version,1,'Fictional start date','text','[]',1,
                       scope,reason,NULL,NULL,0,created_at,updated_at FROM wf_questions WHERE id='q1'""")
        with self.pool.hold('worker', 'apply-2'):
            self.pool.register_questions(self.fixture.processes[1][0], 'apply-2', ['extra'])

    def test_stale_distinct_question_rejects_without_erasing_unrelated_local_draft(self):
        self.add_unrelated_question()
        initial = self.snapshot('iOS')
        stale = self.command(initial, 1, 'iOS', question_id='extra', value='Next month')
        local_drafts = {'extra': 'Next month', 'unrelated-local-note': 'Keep this exact draft.'}
        saved_drafts = copy.deepcopy(local_drafts)
        self.assertEqual(self.request('POST', '/api/v1/answers', self.command(initial))[0], 200)
        status, error = self.request('POST', '/api/v1/answers', stale, 'iOS')
        self.assertEqual(status, 409, error)
        self.assertEqual(error['error']['reason'], 'question_sync_stale')
        self.assertEqual(local_drafts, saved_drafts)
        current = self.snapshot('iOS')
        question = next(q for q in current['questions'] if q['id'] == 'extra')
        self.assertIsNone(question['answer'])
        self.assertEqual(self.answer_count(), 1)
        retry = self.command(current, 1, 'iOS', question_id='extra', value=local_drafts['extra'],
                             request_id='refreshed-extra')
        self.assertEqual(self.request('POST', '/api/v1/answers', retry, 'iOS')[0], 200)
        self.assertEqual(self.answer_count(), 2)

    def test_canonical_answer_overlays_divergent_form_copy_without_promoting_it(self):
        initial = self.snapshot()
        self.assertEqual(self.request('POST', '/api/v1/answers', self.command(initial))[0], 200)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET answer=?,answered_at=? WHERE id='q1'",
                       (json.dumps('no-1'), '2001-01-01T00:00:00+00:00'))
        self.assert_saved(self.snapshot('iOS'))
        with self.store.transaction() as db:
            self.assertEqual(json.loads(db.execute("SELECT answer FROM wf_questions WHERE id='q1'").fetchone()[0]),
                             'no-1')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_profile_answers').fetchone()[0], 1)

    def test_canonical_answer_visible_when_fanout_is_deferred_by_independent_gate(self):
        initial = self.snapshot()
        pid, tid, _ = self.fixture.processes[1]
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (pid,))
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?", (tid,))
        self.assertEqual(self.request('POST', '/api/v1/answers', self.command(initial))[0], 200)
        current = self.snapshot('iOS')
        self.assert_saved(current)
        process = next(p for p in current['processes'] if p['id'] == pid)
        self.assertEqual(process['block_reason'], 'tool_approval_required')
        self.assertEqual(process['task_status'], 'waiting_for_user')
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT answer FROM wf_questions WHERE id='q1'").fetchone()[0])
            self.assertEqual(self.store._task(db, tid)['error'], 'tool_approval_required')


if __name__ == '__main__':
    unittest.main()
