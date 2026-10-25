"""Restart the real backend with fictional SQLite data and local fake workers."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from store import ReviewError, now
from tailscale_backend import BackendConfig, running_backend
from test_readiness import ReadyRuntime
from test_store import catalog
from workflow import WorkflowStore


class RestartRuntime(ReadyRuntime):
    def __init__(self):
        super().__init__()
        self.apply_calls = 0
        self.answers = []

    def apply(self, store, task):
        self.apply_calls += 1
        pid = task['payload']['process_id']
        self.answers = [q['answer'] for q in store.questions() if q['process_id'] == pid]
        store.before_submit(pid, task['lease_token'], 'fictional-form')
        store.confirm_submission(pid, task['lease_token'], {
            'url': 'https://jobs.example/confirmation/fictional',
            'evidence': 'Fictional test receipt; no employer was contacted.',
            'observed_at': now(), 'reference': 'fictional'})


class ServerRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'review.sqlite3'
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        token = secrets.token_urlsafe(32)
        access, credential = self.root / 'access.json', self.root / 'credential.txt'
        access.write_text(json.dumps({'schema_version': 1,
            'public_origin': 'https://fictional.example.ts.net',
            'token_sha256': hashlib.sha256(token.encode()).hexdigest()}))
        credential.write_text(token)
        access.chmod(0o600)
        credential.chmod(0o600)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.config = BackendConfig(self.path, access, credential, port,
                                    self.root / 'fake-runtime.json')

    @contextmanager
    def boot(self, runtime):
        # Disable only feed refill for this queue fixture. All lifecycle,
        # storage recovery, HTTP probes, leases and dispatch code is real.
        with patch('codex_runtime.load_runtime', return_value=runtime), patch(
                'server.WorkflowStore', side_effect=lambda path: WorkflowStore(path, low_watermark=0)):
            with running_backend(self.config) as backend:
                yield backend

    def task(self, task_id):
        return next(t for t in self.store.tasks() if t['id'] == task_id)

    def wait_status(self, task_id, expected):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            task = self.task(task_id)
            if task['status'] == expected:
                return task
            time.sleep(.01)
        self.fail(f"Task {task_id} remained {task['status']}, expected {expected}")

    def enqueue(self, kind, name):
        with self.store.transaction(True) as db:
            return self.store._enqueue_task(db, kind, {'after_explore': True}, name,
                                           resource='browser' if kind == 'explore' else None)

    def authorize(self):
        self.store.import_catalog(catalog(), 'Fictional restart test')
        card = self.store.feed()['cards'][0]
        receipt = self.store.receive_decision({'request_id': 'fictional-apply',
            'action': 'apply', 'card_id': card['id'], 'card_revision': card['revision'],
            'selected': [card['jobs'][0]['key']]})
        task = self.task(receipt['task_ids'][0])
        return task, task['payload']['process_id']

    def ask(self):
        original, pid = self.authorize()
        task = self.store.lease_task('old-worker', kinds=['apply'])
        self.store.start_application(pid, task['lease_token'])
        self.store.ask_questions(pid, task['lease_token'], 'fictional-form', [{
            'id': 'fictional-question', 'text': 'Fictional answer?', 'type': 'text', 'required': True}])
        return original['id'], pid

    def test_ready_and_due_work_resumes_once_while_future_retry_keeps_its_deadline(self):
        explore = self.enqueue('explore', 'queued-explore')
        due = self.enqueue('rank', 'due-rank')
        future = self.enqueue('rank', 'future-rank')
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET status='retry_wait',failure_count=1 WHERE id=?", (due,))
            db.execute("UPDATE wf_tasks SET status='retry_wait',available_at='2999-01-01' WHERE id=?", (future,))
        first = RestartRuntime()
        with self.boot(first):
            self.wait_status(explore, 'succeeded')
            self.wait_status(due, 'succeeded')
            self.assertEqual('retry_wait', self.task(future)['status'])
        second = RestartRuntime()
        with self.boot(second):
            time.sleep(.6)
        self.assertEqual((1, 1), (first.explore_calls, first.rank_calls))
        self.assertEqual((0, 0, 0), (second.probe_calls, second.explore_calls, second.rank_calls))
        self.assertEqual(1, self.task(due)['failure_count'])
        self.assertEqual(0, self.task(future)['attempts'])
        self.assertEqual(3, len(self.store.tasks()))

    def test_answered_application_resumes_same_task_and_completed_receipt_never_replays(self):
        task_id, pid = self.ask()
        process = next(p for p in self.store.processes() if p['id'] == pid)
        answer = {'request_id': 'fictional-answer', 'process_id': pid,
            'process_revision': process['revision'], 'form_version': 'fictional-form',
            'answers': [{'question_id': 'fictional-question', 'question_revision': 1,
                         'value': 'Saved before restart'}]}
        receipt = self.store.answer_questions(answer)
        snapshot = process['snapshot']
        runtime = RestartRuntime()
        with self.boot(runtime):
            self.wait_status(task_id, 'succeeded')
        again = RestartRuntime()
        with self.boot(again):
            time.sleep(.6)
            self.assertEqual(receipt, self.store.answer_questions(answer))
        self.assertEqual(['Saved before restart'], runtime.answers)
        self.assertEqual((1, 0), (runtime.apply_calls, again.apply_calls))
        self.assertEqual(snapshot, self.store.processes()[0]['snapshot'])
        self.assertEqual('submitted', self.store.processes()[0]['submission_status'])
        self.assertEqual(1, len(self.store.tasks()))
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT count(*) FROM wf_submission_attempts').fetchone()[0])

    def test_crashed_rank_recovers_after_cooldown_with_a_new_fence(self):
        task_id = self.enqueue('rank', 'crashed-rank')
        old = self.store.lease_task('old-worker')
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET lease_until='2000-01-01' WHERE id=?", (task_id,))
        runtime = RestartRuntime()
        with self.boot(runtime):
            recovered = self.wait_status(task_id, 'retry_wait')
            self.assertEqual(1, recovered['failure_count'])
            self.assertGreater(recovered['available_at'], now())
            with self.assertRaises(ReviewError):
                self.store.finish_task(task_id, old['lease_token'], {})
            # Advance the fictional retry deadline after checking the cooldown.
            with self.store.transaction(True) as db:
                db.execute("UPDATE wf_tasks SET available_at='2000-01-01' WHERE id=?", (task_id,))
            done = self.wait_status(task_id, 'succeeded')
        self.assertEqual(2, done['attempts'])
        self.assertEqual(1, runtime.rank_calls)

    def test_clean_restart_resumes_interrupted_local_work_without_spending_failure_budget(self):
        from codex_transport import CodexWorkerStopped
        entered, stopped = threading.Event(), threading.Event()
        self.addCleanup(stopped.set)

        class InterruptedRank(RestartRuntime):
            supported_kinds = {'rank'}

            def rank(self, context, lease):
                entered.set()
                if not stopped.wait(5):
                    raise RuntimeError('Fictional test did not stop its worker')
                raise CodexWorkerStopped('Fictional local worker stopped')

            def close(self):
                stopped.set()

        task_id = self.enqueue('rank', 'interrupted-rank')
        with self.boot(InterruptedRank()):
            self.assertTrue(entered.wait(2))
            self.assertEqual('running', self.task(task_id)['status'])
        saved = self.task(task_id)
        self.assertEqual('queued', saved['status'])
        self.assertEqual(0, saved['failure_count'])
        runtime = RestartRuntime()
        with self.boot(runtime):
            done = self.wait_status(task_id, 'succeeded')
        self.assertEqual(2, done['attempts'])
        self.assertEqual(0, done['failure_count'])
        self.assertEqual(1, runtime.rank_calls)

    def assert_crashed_application_is_not_replayed(self, submission_started):
        original, pid = self.authorize()
        old = self.store.lease_task('old-worker', kinds=['apply'])
        self.store.start_application(pid, old['lease_token'])
        if submission_started:
            self.store.before_submit(pid, old['lease_token'], 'fictional-form')
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET lease_until='2000-01-01' WHERE id=?", (old['id'],))
        for _ in range(2):
            runtime = RestartRuntime()
            with self.boot(runtime):
                task = self.wait_status(original['id'], 'waiting_for_user')
                self.assertTrue(task['resource_quarantined'])
                self.assertEqual('submission_unknown' if submission_started else 'worker_lease_lost', task['error'])
                self.assertEqual(1, task['attempts'])
                self.assertEqual((0, 0), (runtime.probe_calls, runtime.apply_calls))
        with self.assertRaises(ReviewError):
            self.store.heartbeat_task(old['id'], old['lease_token'])
        with self.store.transaction() as db:
            self.assertEqual(int(submission_started), db.execute('SELECT count(*) FROM wf_submission_attempts').fetchone()[0])

    def test_crashed_browser_keeps_quarantine_across_repeated_boots(self):
        self.assert_crashed_application_is_not_replayed(False)

    def test_uncertain_submission_keeps_quarantine_and_never_replays(self):
        self.assert_crashed_application_is_not_replayed(True)

    def test_unanswered_questions_and_terminal_tasks_remain_unchanged(self):
        task_id, pid = self.ask()
        expected_questions = self.store.questions()
        terminal = {}
        for state in ('failed', 'cancelled', 'succeeded'):
            tid = self.enqueue('rank', state)
            with self.store.transaction(True) as db:
                self.store._release_task(db, tid, state)
            terminal[tid] = self.task(tid)
        runtime = RestartRuntime()
        with self.boot(runtime):
            time.sleep(.6)
            self.assertEqual('waiting_for_user', self.task(task_id)['status'])
            self.assertEqual('questions_pending', self.task(task_id)['error'])
            self.assertEqual(expected_questions, self.store.questions())
        self.assertEqual((0, 0, 0), (runtime.probe_calls, runtime.apply_calls, runtime.rank_calls))
        for tid, saved in terminal.items():
            self.assertEqual(saved, self.task(tid))


if __name__ == '__main__':
    unittest.main()
