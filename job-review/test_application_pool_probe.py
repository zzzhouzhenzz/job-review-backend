"""Pure probe scheduling/prompt checks. No browser or native process starts."""
import json
import io
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

import application_pool_proof as proof_module
from tools import application_pool_probe as probe
from tools.application_pool_probe import (probe_prompt, schema, stage_schedule, validate_observation,
    run_stage, FixtureGatePhase)
from chrome_access_gate import ChromeAccessGate


class ApplicationPoolProbeTests(unittest.TestCase):
    def setUp(self):
        self.workers = [{'worker_id': f'apply-{number}', 'task_id': str(uuid.uuid4()),
            'thread_id': f'fictional-thread-{number}', 'tab_id': f'fictional-tab-{number}'}
            for number in (1, 2, 3)]

    def test_schedule_runs_two_peers_between_full_phase_barriers(self):
        schedule = stage_schedule(self.workers)
        self.assertEqual([stage for stage, _ in schedule],
                         ['browser_before', 'session_only', 'peer_work', 'browser_after'])
        self.assertEqual([[worker['worker_id'] for worker in workers] for _, workers in schedule],
                         [['apply-1', 'apply-2', 'apply-3'], ['apply-1', 'apply-2', 'apply-3'],
                          ['apply-1', 'apply-2'], ['apply-1', 'apply-2', 'apply-3']])
        self.assertIs(schedule[0][1][0], schedule[3][1][0])
        with self.assertRaises(ValueError):
            probe_prompt(self.workers[2], 'peer_work')

    def test_all_browser_prompts_scope_reads_to_only_own_exact_tab(self):
        for stage, workers in stage_schedule(self.workers):
            if stage == 'session_only':
                continue
            for worker in workers:
                with self.subTest(stage=stage, worker=worker['worker_id']):
                    prompt = probe_prompt(worker, stage, resume=True)
                    self.assertIn(worker['tab_id'], prompt)
                    self.assertIn(worker['task_id'], prompt)
                    self.assertIn('mark this same tab for handoff', prompt)
                    self.assertIn('Do not create, replace or close a tab', prompt)
                    self.assertNotIn('createBrowserTab', prompt)
                    self.assertNotIn('foreign', prompt)
                    self.assertNotIn('isolation check', prompt)
                    for other in self.workers:
                        if other is not worker:
                            self.assertNotIn(other['tab_id'], prompt)
                            self.assertNotIn(other['task_id'], prompt)
                            self.assertNotIn(other['thread_id'], prompt)

    def test_session_only_prompt_and_schema_have_no_browser_operation(self):
        prompt = probe_prompt(self.workers[0], 'session_only')
        self.assertIn('Do not use any tool', prompt)
        self.assertNotIn('getTab', prompt)
        self.assertNotIn('markHandoff', prompt)
        self.assertEqual(set(schema()['properties']), {'worker_id', 'task_id'})
        self.assertNotIn('foreign_tab_checks', schema(True)['properties'])

    def test_creation_requires_explicit_flag_and_replacement_needs_renewed_approval(self):
        worker = self.workers[0]
        with self.assertRaises(ValueError):
            probe_prompt(worker, 'browser_before')
        with self.assertRaises(ValueError):
            probe_prompt(worker, 'browser_before', resume=True, create_new=True)
        with self.assertRaises(ValueError):
            probe_prompt(worker, 'browser_before', resume=True, replace_missing=True)
        new = probe_prompt(worker, 'browser_before', create_new=True)
        self.assertIn('--create-new-fixtures', new)
        self.assertIn('Create exactly ONE new Chrome tab', new)
        approval = 'Fictional renewed approval for this one missing test tab.'
        replacement = probe_prompt(worker, 'browser_before', resume=True, replace_missing=True,
                                   approval_text=approval)
        self.assertIn(json.dumps(approval), replacement)
        self.assertIn('SAME existing native session', replacement)
        self.assertIn('If any NEW approval denial occurs, stop', replacement)

    def observation(self, browser=True):
        worker = self.workers[0]
        value = {'worker_id': worker['worker_id'], 'task_id': worker['task_id']}
        if browser:
            value.update(status='verified', tab_id=worker['tab_id'],
                         owned_tab_ids=[worker['tab_id']], handoff_tab_ids=[worker['tab_id']])
        return SimpleNamespace(result=value, thread_id=worker['thread_id'], turn_completed=True,
                               exit_code=0, browser_verified=browser, tools=['fictional-cua'] if browser else [])

    def test_blocked_or_changed_native_identity_stops_before_next_phase(self):
        worker = self.workers[0]
        for field, value in [('status', 'blocked'), ('tab_id', 'another-tab'),
                             ('handoff_tab_ids', []), ('task_id', 'another-task')]:
            with self.subTest(field=field):
                result = self.observation()
                result.result[field] = value
                with self.assertRaises(RuntimeError):
                    validate_observation(worker, 'browser_after', result)
        result = self.observation()
        result.thread_id = 'another-thread'
        with self.assertRaises(RuntimeError):
            validate_observation(worker, 'browser_before', result)

    def test_replacement_only_accepts_new_tab_in_explicit_first_phase(self):
        result = self.observation()
        result.result.update(tab_id='new-fixture', owned_tab_ids=['new-fixture'], handoff_tab_ids=['new-fixture'])
        with self.assertRaises(RuntimeError):
            validate_observation(self.workers[0], 'browser_before', result)
        validate_observation(self.workers[0], 'browser_before', result, replace_missing=True)
        with self.assertRaises(RuntimeError):
            validate_observation(self.workers[0], 'browser_after', result, replace_missing=True)

    def test_session_only_with_tools_is_rejected(self):
        result = self.observation(browser=False)
        validate_observation(self.workers[0], 'session_only', result)
        result.tools = ['fictional-cua']
        with self.assertRaises(RuntimeError):
            validate_observation(self.workers[0], 'session_only', result)

    def test_concurrent_requests_use_real_gate_fifo_and_one_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gate = ChromeAccessGate(root)
            for stage in ('browser_before', 'peer_work', 'browser_after'):
                workers = dict(stage_schedule(self.workers))[stage]
                closed = threading.Event()
                phase = FixtureGatePhase(gate, stage, workers, closed, barrier_timeout=2)
                calls, active, errors = [], [], []
                first_entered, release_first, second_entered = threading.Event(), threading.Event(), threading.Event()
                guard = threading.Lock()
                def turn(worker, actual_stage):
                    self.assertEqual(actual_stage, stage)
                    with phase.dispatch(worker, root / (stage + worker['worker_id'])) as ticket:
                        with guard:
                            self.assertEqual(active, [])
                            active.append(worker['worker_id'])
                            calls.append(worker['worker_id'])
                            first = len(calls) == 1
                        if first:
                            first_entered.set()
                            self.assertTrue(release_first.wait(2))
                        else:
                            second_entered.set()
                        with guard:
                            active.pop()
                        ticket.verified_terminal()
                runner = threading.Thread(target=lambda: errors.extend(run_stage(stage, workers, turn)))
                runner.start()
                try:
                    self.assertTrue(first_entered.wait(2))
                    self.assertFalse(second_entered.wait(.05))
                    snapshot = gate.snapshot()
                    self.assertEqual(snapshot['state'], 'busy')
                    self.assertEqual(len(snapshot['queue']), len(workers)-1)
                    self.assertEqual(len(phase.record['queued']['queue']), len(workers))
                    self.assertIsNone(phase.record['queued']['owner'])
                finally:
                    release_first.set()
                    runner.join(3)
                self.assertFalse(runner.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(calls, [item['worker_id'] for item in phase.record['queued']['queue']])
                self.assertEqual(phase.evidence()['final']['state'], 'free')
                self.assertEqual(phase.evidence()['final']['queue'], [])
                self.assertEqual(phase.record['gate_directory'], str((root / '_chrome_access').resolve()))

    def test_browser_failure_keeps_owner_uncertain_and_cancels_waiters(self):
        for verified in (False, None):
            with self.subTest(verified=verified), tempfile.TemporaryDirectory() as folder:
                gate = ChromeAccessGate(Path(folder))
                closed = threading.Event()
                phase = FixtureGatePhase(gate, 'browser_before', self.workers, closed, barrier_timeout=2)
                calls = []
                def denied(worker, stage):
                    with phase.dispatch(worker, Path(folder) / worker['worker_id']):
                        calls.append(worker['worker_id'])
                        if verified is False:
                            raise RuntimeError('fictional tool denial')
                        # Returning without validated completion must also retain ownership.
                errors = run_stage('browser_before', self.workers, denied)
                self.assertEqual(len(errors), 3)
                self.assertEqual(len(calls), 1)
                self.assertTrue(closed.is_set())
                snapshot = gate.snapshot()
                self.assertEqual(snapshot['state'], 'uncertain')
                self.assertEqual(snapshot['owner']['worker_id'], calls[0])
                self.assertEqual(snapshot['queue'], [])

    def test_failed_enqueue_barrier_stops_without_starting_native(self):
        with tempfile.TemporaryDirectory() as folder:
            gate = ChromeAccessGate(Path(folder))
            closed = threading.Event()
            phase = FixtureGatePhase(gate, 'browser_before', self.workers, closed, barrier_timeout=.05)
            calls = []
            def turn(worker, stage):
                with phase.dispatch(worker, Path(folder) / 'never-native'):
                    calls.append(worker)
            self.assertEqual(len(run_stage('browser_before', self.workers[:1], turn)), 1)
            self.assertEqual(calls, [])
            self.assertTrue(closed.is_set())
            self.assertEqual(gate.snapshot()['state'], 'free')
            self.assertEqual(gate.snapshot()['queue'], [])

    def test_only_tool_free_phase_can_run_native_turns_together(self):
        barrier = threading.Barrier(3)
        entered = set()
        guard = threading.Lock()
        def turn(worker, stage):
            self.assertEqual(stage, 'session_only')
            with guard:
                entered.add(worker['worker_id'])
            barrier.wait(2)
            self.assertEqual(entered, {'apply-1', 'apply-2', 'apply-3'})
        self.assertEqual(run_stage('session_only', self.workers, turn), [])

    def test_complete_probe_uses_private_gate_and_verifies_real_ticket_receipts(self):
        self.run_fictional_probe(blocked=False)

    def test_complete_probe_denial_leaves_uncertain_owner_and_no_proof(self):
        self.run_fictional_probe(blocked=True)

    def test_probe_rejects_runtime_gate_outside_output_before_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'fictional-runtime.json'
            config.write_text(json.dumps({'run_directory': str(root / 'protected-runs')}))
            with patch.object(probe, 'load_runtime') as load, patch('sys.argv', ['probe',
                    '--runtime', str(config), '--output-directory', str(root / 'output'),
                    '--create-new-fixtures']):
                with self.assertRaisesRegex(ValueError, 'must equal'):
                    probe.main()
                load.assert_not_called()
            self.assertFalse((root / 'protected-runs').exists())
            self.assertFalse((root / 'output').exists())

    def run_fictional_probe(self, *, blocked):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            output = root / 'probe'
            production_runs = root / 'must-not-use-runtime-gate'
            runtime = SimpleNamespace(transport=SimpleNamespace(binary=root / 'never-started', cwd=root,
                timeout=5, heartbeat_interval=.1, run_directory=output),
                pool_configuration_identity=lambda: {}, close=Mock())
            (root / 'fictional-runtime.json').write_text(json.dumps({'run_directory': str(output)}))
            def load_runtime(path):
                runtime.transport.chrome_access_gate = ChromeAccessGate(output)
                return runtime
            calls, native_threads = [], {worker: str(uuid.uuid4()) for worker in ('apply-1', 'apply-2', 'apply-3')}
            tool_free_barrier = threading.Barrier(3)
            guard = threading.Lock()
            active = []
            def native(**kwargs):
                worker = re.search(r'worker_id(?: is |=)(apply-[123])', kwargs['prompt']).group(1)
                task = re.search(r'task_id(?: is |=)([0-9a-f-]{36})', kwargs['prompt']).group(1)
                browser = kwargs['require_browser']
                with guard:
                    calls.append((worker, browser))
                    if browser:
                        self.assertEqual(active, [])
                        active.append(worker)
                state = ChromeAccessGate(output).snapshot()
                if browser:
                    self.assertEqual(state['owner']['worker_id'], worker)
                    self.assertEqual(state['owner']['task_id'], task)
                    self.assertEqual(state['owner']['state'], 'active')
                    self.assertTrue(state['owner']['acquired_at'])
                    self.assertTrue(state['owner']['started_at'])
                else:
                    self.assertIsNone(state['owner'])
                    tool_free_barrier.wait(2)
                value = {'worker_id': worker, 'task_id': task}
                if browser:
                    tab = 'fictional-' + worker
                    value.update(status='blocked' if blocked else 'verified', detail='Fictional fixture only',
                                 tab_id=tab, owned_tab_ids=[tab], handoff_tab_ids=[] if blocked else [tab])
                    with guard:
                        active.pop()
                return SimpleNamespace(result=value, thread_id=native_threads[worker], turn_id=str(uuid.uuid4()),
                    turn_completed=True, exit_code=0, browser_verified=browser,
                    tools={('cua', 'js')} if browser else set(), approval_events=[])
            identity = {'fictional': True}
            with patch.object(probe, 'load_runtime', side_effect=load_runtime), \
                 patch.object(probe, 'collect_identity', return_value=identity), \
                 patch.object(probe, 'run_appserver', side_effect=native), \
                 patch.object(probe, 'fingerprint', return_value='f'*64), \
                 patch.object(proof_module, '_current', return_value=None) as current_identity, \
                 patch('sys.stdout', new_callable=io.StringIO), \
                 patch('sys.argv', ['probe', '--runtime', str(root / 'fictional-runtime.json'),
                                   '--output-directory', str(output), '--create-new-fixtures']):
                if blocked:
                    with self.assertRaises(RuntimeError):
                        probe.main()
                else:
                    probe.main()
                    document = json.loads((output / 'proof.json').read_text())
                    proof_module._verify(document, identity)
                    # Only host identity is fictional. Receipt, handoff and FIFO
                    # checks above remain the real production validators.
                    self.assertEqual(current_identity.call_count, 2)
                    current_identity.assert_called_with(identity)
                if blocked:
                    current_identity.assert_not_called()
            runtime.close.assert_called_once()
            self.assertFalse(production_runs.exists())
            state = ChromeAccessGate(output).snapshot()
            self.assertEqual(state['queue'], [])
            if blocked:
                self.assertEqual(len(calls), 1)
                self.assertEqual(state['state'], 'uncertain')
                self.assertFalse((output / 'proof.json').exists())
            else:
                self.assertEqual(len(calls), 11)
                self.assertEqual(sum(browser for _, browser in calls), 8)
                self.assertEqual(state['state'], 'free')
                for stage, expected in (('browser_before', 3), ('peer_work', 2), ('browser_after', 3)):
                    evidence = json.loads((output / (stage + '-chrome-access.json')).read_text())
                    self.assertEqual(len(evidence['queued']['queue']), expected)
                    self.assertEqual(evidence['gate_directory'], str(output / '_chrome_access'))


if __name__ == '__main__':
    unittest.main()
