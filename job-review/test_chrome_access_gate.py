"""Fictional host concurrency and crash tests. No Chrome or production state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from chrome_access_gate import ChromeAccessGate
from codex_transport import CodexTransport, CodexTransportError, CodexWorkerStopped
from codex_appserver import AppServerError, AppServerResult
from readiness import BrowserRecoverable, BrowserWorkUncertain
from orchestrator import TaskLease
import test_application_pool as pool_fixtures


def until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Fictional worker did not reach the expected state.')


class Lease:
    def __init__(self, worker=None):
        self.task_id, self.token = str(uuid.uuid4()), str(uuid.uuid4())
        self.application_worker_id = worker
        self.beats = 0
        self.stages = []

    def heartbeat(self):
        self.beats += 1
        return True

    def stage(self, name):
        self.stages.append(name)


class ChromeGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gate = ChromeAccessGate(self.root)
        self.closed = threading.Event()
        self.addCleanup(self.closed.set)
        self.errors = []
        self.threads = []
        self.addCleanup(self.join)

    def join(self):
        self.closed.set()
        for thread in self.threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in self.threads))

    def thread(self, action):
        def call():
            try:
                action()
            except BaseException as error:
                self.errors.append(error)
        thread = threading.Thread(target=call)
        self.threads.append(thread)
        thread.start()
        return thread

    def acquire(self, lease, role='application_prepare'):
        return self.gate.acquire(lease, role, self.closed, .02)

    def test_fifo_includes_discovery_and_reentrant_owner_cannot_starve_waiters(self):
        first, second, third = Lease('apply-1'), Lease('apply-2'), Lease()
        owner = self.acquire(first)
        order = []
        releases = [threading.Event(), threading.Event(), threading.Event()]
        self.addCleanup(lambda: [event.set() for event in releases])
        def wait(lease, name, release):
            ticket = self.acquire(lease, 'explorer' if name == 'discovery' else 'application_prepare')
            order.append(name)
            release.wait(3)
            ticket.finish(safe=True)
        self.thread(lambda: wait(second, 'second', releases[0]))
        until(lambda: len(self.gate.snapshot()['queue']) == 1)
        self.thread(lambda: wait(third, 'discovery', releases[1]))
        until(lambda: len(self.gate.snapshot()['queue']) == 2)
        self.assertEqual([second.task_id, third.task_id], [v['task_id'] for v in self.gate.snapshot()['queue']])
        owner.finish(safe=True)
        until(lambda: order == ['second'])
        self.thread(lambda: wait(first, 'first-again', releases[2]))
        until(lambda: len(self.gate.snapshot()['queue']) == 2)
        releases[0].set()
        until(lambda: order == ['second', 'discovery'])
        releases[1].set()
        until(lambda: order == ['second', 'discovery', 'first-again'])
        releases[2].set()
        self.join()
        self.assertEqual([], self.errors)
        self.assertEqual('free', self.gate.snapshot()['state'])

    def test_wait_heartbeats_without_timeout_then_cancels_before_native_start(self):
        owner = self.acquire(Lease('apply-1'))
        lease = Lease('apply-2')
        self.thread(lambda: self.acquire(lease))
        until(lambda: lease.beats >= 4)
        self.assertEqual(['waiting_for_browser'], lease.stages)
        self.closed.set()
        self.join()
        self.assertEqual(1, len(self.errors))
        self.assertIsInstance(self.errors[0], CodexWorkerStopped)
        snapshot = self.gate.snapshot()
        self.assertEqual([], snapshot['queue'])
        self.assertEqual(owner.id, snapshot['owner']['ticket_id'])
        owner.finish(safe=True)

    def test_uncertain_owner_persists_across_gate_restart(self):
        owner = self.acquire(Lease('apply-1'))
        owner.native_started(self.root / 'fictional-run')
        owner.finish(safe=False)
        restarted = ChromeAccessGate(self.root)
        self.assertEqual('uncertain', restarted.snapshot()['state'])
        self.thread(lambda: restarted.acquire(Lease('apply-2'), 'application_prepare', self.closed, .02))
        until(lambda: len(restarted.snapshot()['queue']) == 1)
        self.closed.set()
        self.join()
        self.assertEqual(owner.id, restarted.snapshot()['owner']['ticket_id'])

    def test_duplicate_live_owner_is_busy_but_uncertain_or_dead_owner_is_not_retryable(self):
        lease = Lease('apply-1')
        owner = self.acquire(lease)
        with self.assertRaises(BrowserRecoverable):
            self.acquire(lease)
        owner.native_started(self.root / 'fictional-native')
        owner.finish(safe=False)
        with self.assertRaises(BrowserWorkUncertain):
            self.acquire(lease)
        self.assertEqual(owner.id, self.gate.snapshot()['owner']['ticket_id'])
        # A crashed host can leave the on-disk owner active rather than uncertain.
        with self.gate._state() as value:
            value['owner']['state'] = 'active'
        with self.assertRaises(BrowserWorkUncertain):
            self.acquire(lease)
        self.assertEqual('uncertain', self.gate.snapshot()['state'])

    def test_real_host_process_exit_cannot_clear_native_owner(self):
        code = '''import os,sys,threading,uuid
from types import SimpleNamespace
from pathlib import Path
from chrome_access_gate import ChromeAccessGate
lease=SimpleNamespace(task_id=str(uuid.uuid4()),token=str(uuid.uuid4()),heartbeat=lambda:True)
ticket=ChromeAccessGate(Path(sys.argv[1])).acquire(lease,'explorer',threading.Event(),.02)
ticket.native_started(Path('fictional-crashed-native-turn'))
os._exit(0)
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)],
                                cwd=Path(__file__).parent, capture_output=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('uncertain', self.gate.snapshot()['state'])
        self.assertEqual('active', json.loads(self.gate.path.read_text())['owner']['state'])

    def test_dead_waiter_is_removed_but_live_owner_is_not(self):
        owner = self.acquire(Lease('apply-1'))
        code = '''import sys,threading,uuid
from types import SimpleNamespace
from pathlib import Path
from chrome_access_gate import ChromeAccessGate
lease=SimpleNamespace(task_id=str(uuid.uuid4()),token=str(uuid.uuid4()),heartbeat=lambda:True)
ChromeAccessGate(Path(sys.argv[1])).acquire(lease,'explorer',threading.Event(),.02)
'''
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root)], cwd=Path(__file__).parent)
        try:
            until(lambda: len(self.gate.snapshot()['queue']) == 1)
        finally:
            child.terminate()
            child.wait(3)
        next_lease = Lease('apply-3')
        acquired = []
        def run():
            ticket = self.acquire(next_lease)
            acquired.append(ticket.id)
            ticket.finish(safe=True)
        self.thread(run)
        until(lambda: [v['task_id'] for v in self.gate.snapshot()['queue']] == [next_lease.task_id])
        self.assertEqual(owner.id, self.gate.snapshot()['owner']['ticket_id'])
        owner.finish(safe=True)
        until(lambda: bool(acquired))
        self.join()
        self.assertEqual([], self.errors)

    def test_legacy_quarantine_prevents_admission_without_becoming_new_owner(self):
        lease = Lease('apply-1')
        lease.browser_gate_blockers = lambda: [{'task_id': 'legacy-owner', 'resource': 'browser:apply-3'}]
        self.thread(lambda: self.acquire(lease))
        until(lambda: self.gate.snapshot()['state'] == 'blocked')
        self.assertIsNone(self.gate.snapshot()['owner'])
        self.assertGreater(lease.beats, 0)
        self.closed.set()
        self.join()
        self.assertEqual('blocked', self.gate.snapshot()['state'])

    def test_transport_releases_chrome_before_slow_question_model(self):
        transport = CodexTransport(Path('/usr/bin/true'), self.root / 'runs', cwd=self.root,
                                   timeout=.01, heartbeat_interval=.02)
        self.addCleanup(transport.close)
        model_entered, model_release = threading.Event(), threading.Event()
        self.addCleanup(model_release.set)
        def native(role, prompt, schema, lease, *, browser, session_worker):
            if browser:
                lease._chrome_gate_ticket.native_started(self.root / role)
                lease._chrome_gate_ticket.verified_terminal()
            else:
                model_entered.set()
                model_release.wait(3)
            return {'ok': True}
        with patch.object(transport, '_run_unguarded', side_effect=native):
            transport.run('application_prepare', 'Fictional', {}, Lease('apply-1'), browser=True)
            self.thread(lambda: transport.run('question_match', 'Fictional', {}, Lease('apply-1'), session_worker=True))
            self.assertTrue(model_entered.wait(2))
            self.assertEqual('free', transport.chrome_access_gate.snapshot()['state'])
            self.assertEqual({'ok': True}, transport.run('explorer', 'Fictional', {}, Lease(), browser=True))
            model_release.set()
            self.join()
        self.assertEqual([], self.errors)

    def test_transport_safe_failure_releases_but_uncertain_failure_keeps_owner(self):
        transport = CodexTransport(Path('/usr/bin/true'), self.root / 'runs', cwd=self.root)
        with patch.object(transport, '_run_unguarded', side_effect=ValueError('Before native')):
            with self.assertRaises(ValueError):
                transport.run('explorer', 'Fictional', {}, Lease(), browser=True)
        self.assertEqual('free', transport.chrome_access_gate.snapshot()['state'])
        with patch.object(transport, '_run_unguarded', side_effect=BrowserWorkUncertain('Unproved old native owner')):
            with self.assertRaises(BrowserWorkUncertain):
                transport.run('explorer', 'Fictional', {}, Lease(), browser=True)
        self.assertEqual('uncertain', transport.chrome_access_gate.snapshot()['state'])

    def test_cli_spawn_failure_before_any_child_releases_capacity(self):
        transport = CodexTransport(Path('/usr/bin/true'), self.root / 'runs', cwd=self.root)
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        with patch('codex_transport.subprocess.Popen', side_effect=OSError('Fictional spawn failure')):
            with self.assertRaises(CodexTransportError):
                transport.run('explorer', 'Fictional', schema, Lease(), browser=True)
        self.assertEqual('free', transport.chrome_access_gate.snapshot()['state'])

    def test_native_failure_before_turn_releases_capacity(self):
        transport = CodexTransport(Path('/usr/bin/true'), self.root / 'runs', cwd=self.root,
                                   browser_transport='app-server')
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        with patch('codex_appserver.run_appserver', side_effect=AppServerError('Fictional pre-turn failure', uncertain=False)):
            with self.assertRaises(CodexTransportError):
                transport.run('explorer', 'Fictional', schema, Lease(), browser=True)
        self.assertEqual('free', transport.chrome_access_gate.snapshot()['state'])

    def test_session_completion_save_failure_keeps_chrome_uncertain(self):
        transport = CodexTransport(Path('/usr/bin/true'), self.root / 'runs', cwd=self.root,
                                   browser_transport='app-server')
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        native = AppServerResult({}, str(uuid.uuid4()), True, True, (('cua_repl', 'js'),), 1, 0,
                                 turn_id=str(uuid.uuid4()))
        with patch('codex_appserver.run_appserver', return_value=native), \
             patch('codex_transport._BrowserSession.completed', side_effect=OSError('Fictional disk failure')):
            with self.assertRaises(BrowserWorkUncertain):
                transport.run('explorer', 'Fictional', schema, Lease(), browser=True)
        self.assertEqual('uncertain', transport.chrome_access_gate.snapshot()['state'])


class ChromeGateStoreTests(unittest.TestCase):
    setUp = pool_fixtures.ApplicationPoolTests.setUp
    tearDown = pool_fixtures.ApplicationPoolTests.tearDown

    def test_wait_preserves_lease_budget_and_allows_independent_database_writes(self):
        gate = ChromeAccessGate(Path(self.tmp.name))
        self.store.browser_access_snapshot = gate.snapshot
        self.store.browser_access_serialized = True
        tasks = [self.store.lease_task('fictional-owner', ['apply'], application_worker_id=f'apply-{n}')
                 for n in (1, 2, 3)]
        owner = gate.acquire(TaskLease(self.store, tasks[0]), 'application_prepare', threading.Event(), .02)
        closed, errors = threading.Event(), []
        lease = TaskLease(self.store, tasks[1])
        def wait():
            try:
                gate.acquire(lease, 'application_prepare', closed, .02)
            except CodexWorkerStopped:
                pass
            except Exception as error:
                errors.append(error)
        waiter = threading.Thread(target=wait)
        waiter.start()
        try:
            until(lambda: len(gate.snapshot()['queue']) == 1)
            # A different application can persist a real question while Chrome
            # waits; the queue cannot retain a SQLite write transaction.
            self.store.start_application(tasks[2]['payload']['process_id'], tasks[2]['lease_token'])
            self.store.ask_questions(tasks[2]['payload']['process_id'], tasks[2]['lease_token'], 'fictional-form',
                [{'id': 'q', 'text': 'Fictional missing fact?', 'type': 'text', 'required': True}])
            time.sleep(.08)
            with self.store.transaction() as db:
                current = self.store._task(db, tasks[1]['id'])
                self.assertEqual('running', current['status'])
                self.assertEqual(tasks[1]['lease_token'], current['lease_token'])
                self.assertEqual(tasks[1]['attempts'], current['attempts'])
                self.assertEqual(tasks[1]['failure_count'], current['failure_count'])
                self.assertGreater(current['lease_until'], tasks[1]['lease_until'])
            workflow = self.store.application_workflow()
            self.assertEqual('waiting_for_browser', workflow['workers'][1]['stage'])
            self.assertEqual(tasks[0]['id'], workflow['browser_access']['owner']['task_id'])
            self.assertEqual([tasks[1]['id']], [v['task_id'] for v in workflow['browser_access']['queue']])
            self.assertEqual(1, workflow['question_pool']['pending_count'])
        finally:
            closed.set()
            waiter.join(3)
            owner.finish(safe=True)
        self.assertFalse(waiter.is_alive())
        self.assertEqual([], errors)

    def test_discovery_can_lease_beside_pool_only_when_host_gate_is_attached(self):
        self.store.browser_access_serialized = True
        apply = self.store.lease_task('fictional-owner', ['apply'], application_worker_id='apply-1')
        with self.store.transaction(True) as db:
            discovery = self.store._enqueue_task(db, 'explore', {}, 'fictional-discovery', resource='browser')
        other = self.store.lease_task('fictional-discovery-owner', ['explore'])
        self.assertEqual(discovery, other['id'])
        self.assertIsNotNone(self.store.lease_task('fictional-owner', ['apply'], application_worker_id='apply-2'))
        with self.store.transaction() as db:
            self.assertEqual('running', self.store._task(db, apply['id'])['status'])

    def test_quarantine_is_global_blocker_but_submission_outcome_alone_is_not(self):
        gate = ChromeAccessGate(Path(self.tmp.name))
        self.store.browser_access_snapshot = gate.snapshot
        one = self.store.lease_task('fictional-owner', ['apply'], application_worker_id='apply-1')
        two = self.store.lease_task('fictional-owner', ['apply'], application_worker_id='apply-2')
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE task_id=?", (one['id'],))
        self.assertEqual([], TaskLease(self.store, two).browser_gate_blockers())
        self.store.quarantine_task(one['id'], one['lease_token'], 'browser_work_uncertain')
        blocked = TaskLease(self.store, two).browser_gate_blockers()
        self.assertEqual([{'task_id': one['id'], 'resource': 'browser:apply-1'}], blocked)
        access = self.store.application_workflow()['browser_access']
        self.assertEqual('blocked', access['state'])
        self.assertEqual(blocked, access['blocked_by'])
        self.assertIsNone(access['owner'])


if __name__ == '__main__':
    unittest.main()
