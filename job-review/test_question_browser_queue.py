"""Question answers resume a logical worker at the Chrome FIFO tail.

Real host gate, application leases and answer workflow; native browser calls
are mocked. All application data and gate files are fictional and temporary.
"""
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_application_pool as fixtures
from test_chrome_access_gate import until
from codex_transport import CodexTransport
from orchestrator import TaskLease
from question_pool import SharedQuestionPool
from question_sync import snapshot


class QuestionBrowserQueueTests(unittest.TestCase):
    def test_answered_worker_rejoins_tail_after_safe_terminal_releases_chrome(self):
        fixture = fixtures.ApplicationPoolTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        store = fixture.store
        pool = store.question_pool = SharedQuestionPool(store).initialize()
        transport = CodexTransport(Path('/usr/bin/true'), Path(fixture.tmp.name) / 'runs',
                                   cwd=Path(fixture.tmp.name), heartbeat_interval=.02)
        gate = transport.chrome_access_gate
        tasks = [store.lease_task('fictional-host', ['apply'], application_worker_id=f'apply-{i}')
                 for i in (1, 2, 3)]
        for task in tasks:
            store.start_application(task['payload']['process_id'], task['lease_token'])
        releases = {key: threading.Event() for key in ('first', 'second', 'third', 'resumed')}
        entered = {key: threading.Event() for key in releases}
        paused = threading.Event()
        order, failures, threads = [], [], []

        def cleanup():
            for event in releases.values(): event.set()
            transport.close()
            for thread in threads: thread.join(3)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.addCleanup(cleanup)

        def native(role, prompt, schema, lease, *, browser, session_worker):
            self.assertTrue(browser)
            label = prompt
            ticket = lease._chrome_gate_ticket
            ticket.native_started(Path(fixture.tmp.name) / label)
            order.append(lease.application_worker_id)
            entered[label].set()
            if not releases[label].wait(3):
                raise AssertionError('Fictional native release timed out.')
            # The mock represents a completed native turn with validated handoff.
            ticket.verified_terminal()
            return {'outcome': 'questions' if label == 'first' else 'ready'}

        def start(action):
            def guarded():
                try: action()
                except BaseException as error: failures.append(error)
            thread = threading.Thread(target=guarded)
            threads.append(thread); thread.start()

        def first_worker():
            task = tasks[0]
            result = transport.run('application_prepare', 'first', {}, TaskLease(store, task), browser=True)
            self.assertEqual(result['outcome'], 'questions')
            self.assertNotEqual(gate.snapshot()['owner']['task_id'] if gate.snapshot()['owner'] else None, task['id'])
            with pool.hold('worker', 'apply-1'):
                store.ask_questions(task['payload']['process_id'], task['lease_token'], 'fixture-form',
                    [{'id': 'fixture-required', 'text': 'Fictional preferred start date',
                      'type': 'text', 'required': True}])
                pool.register_questions(task['payload']['process_id'], 'apply-1', ['fixture-required'])
            store.refresh_application_slot('apply-1')
            paused.set()

        with patch.object(transport, '_run_unguarded', side_effect=native):
            start(first_worker)
            self.assertTrue(entered['first'].wait(2))
            start(lambda: transport.run('application_prepare', 'second', {}, TaskLease(store, tasks[1]), browser=True))
            until(lambda: [q['worker_id'] for q in gate.snapshot()['queue']] == ['apply-2'])
            start(lambda: transport.run('application_prepare', 'third', {}, TaskLease(store, tasks[2]), browser=True))
            until(lambda: [q['worker_id'] for q in gate.snapshot()['queue']] == ['apply-2', 'apply-3'])
            releases['first'].set()
            self.assertTrue(paused.wait(2))
            self.assertTrue(entered['second'].wait(2))
            self.assertEqual(gate.snapshot()['owner']['worker_id'], 'apply-2')
            self.assertEqual([q['worker_id'] for q in gate.snapshot()['queue']], ['apply-3'])
            self.assertIsNone(store.lease_task('waiting', ['apply'], application_worker_id='apply-1'))

            def formatter(specification, payload):
                # Normal answer formatting and the shared writer do not acquire
                # or wait for Chrome; worker2 still owns it throughout this save.
                self.assertEqual(gate.snapshot()['owner']['worker_id'], 'apply-2')
                self.assertEqual([q['worker_id'] for q in gate.snapshot()['queue']], ['apply-3'])
                return specification['append_text']
            store.answer_formatter = formatter
            ready = snapshot(store)
            question = ready['questions'][0]
            process = next(p for p in ready['processes'] if p['id'] == question['process_id'])
            receipt = store.answer_questions({'request_id': 'fictional-user-answer', 'process_id': process['id'],
                'process_revision': process['revision'], 'form_version': question['form_version'],
                'expected_pool_revision': ready['sync']['pool_revision'],
                'answers': [{'question_id': question['id'], 'question_revision': question['revision'],
                             'value': 'Next month'}]})
            self.assertEqual(receipt['task_ids'], [tasks[0]['id']])
            resumed = store.lease_task('same-host', ['apply'], application_worker_id='apply-1')
            self.assertEqual(resumed['id'], tasks[0]['id'])
            self.assertEqual(resumed['checkpoint']['application_worker_id'], 'apply-1')
            start(lambda: transport.run('application_prepare', 'resumed', {}, TaskLease(store, resumed), browser=True))
            until(lambda: [q['worker_id'] for q in gate.snapshot()['queue']] == ['apply-3', 'apply-1'])
            self.assertFalse(entered['resumed'].is_set())
            releases['second'].set()
            self.assertTrue(entered['third'].wait(2))
            self.assertFalse(entered['resumed'].is_set())
            releases['third'].set()
            self.assertTrue(entered['resumed'].wait(2))
            releases['resumed'].set()
            for thread in threads: thread.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(order, ['apply-1', 'apply-2', 'apply-3', 'apply-1'])
        self.assertEqual(gate.snapshot()['state'], 'free')
        self.assertEqual(gate.snapshot()['queue'], [])
        self.assertEqual(snapshot(store)['shared_questions'][0]['status'], 'answered')


if __name__ == '__main__':
    unittest.main()
