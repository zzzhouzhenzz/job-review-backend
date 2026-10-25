"""Fictional native receipts only; no Chrome or employer actions."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import test_shared_browser_session as fixtures
from codex_appserver import AppServerResult
from codex_transport import CodexTransport, CodexTransportError, SessionOnlyHandoffUnverified, _replace_private_json
from readiness import BrowserRecoverable, BrowserWorkUncertain


class PoolSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / 'runs'
        self.state = fixtures.State(self.root / 'state.sqlite')
        self.transport = CodexTransport(Path('/usr/bin/true'), self.runs, cwd=self.root,
            browser_transport='app-server', browser_session_policy='shared')
        self.addCleanup(self.transport.close)
        self.calls = []
        self.native = patch('codex_appserver.run_appserver', side_effect=self.result).start()
        self.addCleanup(patch.stopall)
        patch('codex_appserver.archive_worker_thread').start()

    def result(self, **kwargs):
        self.calls.append(kwargs)
        browser = kwargs.get('require_browser', True)
        return AppServerResult({'ok': True}, kwargs['thread_id'] or str(uuid.uuid4()),
            True, browser, (('cua_repl', 'js'),) if browser else (), 100, 0,
            turn_id=str(uuid.uuid4()))

    def lease(self, worker):
        lease = self.state.lease()
        lease.application_worker_id = worker
        lease.verify_pool_transport=lambda: None  # Fictional host proof for transport unit tests.
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        return lease

    def test_missing_host_verifier_fails_before_native_call(self):
        lease=self.lease('apply-1')
        del lease.verify_pool_transport
        with self.assertRaises(SessionOnlyHandoffUnverified):
            self.run_turn(lease)
        self.assertFalse(self.calls)

    def test_host_tab_admission_rejection_prevents_native_call(self):
        lease=self.lease('apply-1')
        def reject(role,thread_id):
            raise CodexTransportError('Fictional foreign tab/thread rejected before dispatch')
        lease.admit_application_tab=reject
        with self.assertRaises(CodexTransportError):self.run_turn(lease)
        self.assertFalse(self.calls)

    def test_missing_host_tab_admission_fails_before_native_call(self):
        lease=self.lease('apply-1');del lease.admit_application_tab
        with self.assertRaises(CodexTransportError):self.run_turn(lease)
        self.assertFalse(self.calls)

    def run_turn(self, lease, role='application_prepare', *, model=False):
        return self.transport.run(role, 'Fictional current job.', fixtures.SCHEMA, lease,
                                  browser=not model, session_worker=model)

    def record(self, worker):
        return json.loads((self.runs / '_browser_sessions' / (worker + '.json')).read_text())

    def test_three_slots_serialize_native_calls_and_keep_distinct_sessions(self):
        active = 0
        maximum = 0
        guard = threading.Lock()
        errors = []
        leases = [self.lease('apply-' + str(n)) for n in range(1, 4)]
        def native(**kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(active, maximum)
            try:
                time.sleep(.03)
                return self.result(**kwargs)
            finally:
                with guard: active -= 1
        def run(lease):
            try:
                self.run_turn(lease)
            except BaseException as error:
                errors.append(error)
        self.native.side_effect = native
        threads = [threading.Thread(target=run, args=(lease,)) for lease in leases]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(1, maximum)
        identities = [self.record(lease.application_worker_id)['record']['thread_id'] for lease in leases]
        self.assertEqual(3, len(set(identities)))
        self.assertEqual(3, sum(call['thread_id'] is None for call in self.calls))

    def test_same_slot_cannot_run_two_native_calls(self):
        entered, release = threading.Event(), threading.Event()
        lease = self.lease('apply-1')
        errors = []
        def native(**kwargs):
            entered.set()
            if not release.wait(3): raise AssertionError('Test release not received.')
            return self.result(**kwargs)
        def run():
            try: self.run_turn(lease)
            except BaseException as error: errors.append(error)
        self.native.side_effect = native
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(BrowserRecoverable): self.run_turn(lease)
            self.assertEqual(1, self.native.call_count)
        finally:
            release.set()
            thread.join(4)
        self.assertEqual([], errors)

    def test_readiness_prepare_submit_and_restart_keep_one_identity(self):
        lease = self.lease('apply-2')
        for role in ('linkedin_session', 'application_prepare', 'application_submit'):
            self.run_turn(lease, role)
        identity = self.record('apply-2')['record']['thread_id']
        self.assertEqual([None, identity, identity], [call['thread_id'] for call in self.calls])
        self.transport = CodexTransport(Path('/usr/bin/true'), self.runs, cwd=self.root,
            browser_transport='app-server', browser_session_policy='shared')
        self.addCleanup(self.transport.close)
        self.run_turn(lease)
        self.assertEqual(identity, self.calls[-1]['thread_id'])
        self.assertFalse((self.runs / '_browser_sessions' / '_shared.json').exists())

    def test_session_only_requires_handoff_proof_and_preserves_browser_receipt(self):
        lease = self.lease('apply-1')
        self.run_turn(lease)
        before = self.record('apply-1')
        with self.assertRaises(SessionOnlyHandoffUnverified):
            self.run_turn(lease, 'application_question_match', model=True)
        self.assertEqual(before, self.record('apply-1'))
        self.assertEqual(1, self.native.call_count)
        lease.session_only_handoff_verified = True
        self.run_turn(lease, 'application_question_match', model=True)
        value = self.record('apply-1')
        self.assertEqual(before['record'], value['browser_record'])
        self.assertEqual('session_only', value['record']['mode'])
        self.assertEqual(before['record']['thread_id'], value['record']['thread_id'])
        self.assertFalse(self.calls[-1]['require_browser'])
        self.run_turn(lease)
        self.assertEqual(before['record']['thread_id'], self.calls[-1]['thread_id'])

    def test_session_only_can_start_before_any_browser_draft(self):
        lease = self.lease('apply-3')
        self.run_turn(lease, 'application_question_match', model=True)
        identity = self.record('apply-3')['record']['thread_id']
        self.run_turn(lease, 'application_prepare')
        self.assertEqual(identity, self.calls[-1]['thread_id'])

    def test_unproved_active_marker_blocks_shared_chrome_until_stopped_review(self):
        lease = self.lease('apply-1')
        self.run_turn(lease)
        path = self.runs / '_browser_sessions' / 'apply-1.json'
        value = self.record('apply-1')
        value['record']['status'] = 'active'
        _replace_private_json(path, value)
        with self.assertRaises(BrowserWorkUncertain): self.run_turn(lease)
        snapshot = self.transport.chrome_access_gate.snapshot()
        self.assertEqual('uncertain', snapshot['state'])
        self.assertEqual(lease.task_id, snapshot['owner']['task_id'])
        self.assertEqual(1, self.native.call_count)

    def test_same_task_cannot_move_to_another_slot(self):
        lease = self.lease('apply-1')
        self.run_turn(lease)
        lease.application_worker_id = 'apply-2'
        lease.verify_pool_transport=lambda: None  # Fictional host verification.
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        with self.assertRaises(BrowserRecoverable): self.run_turn(lease)
        self.assertEqual(1, self.native.call_count)

    def test_paused_assignment_retained_terminal_assignment_can_rotate(self):
        first, second = self.lease('apply-1'), self.lease('apply-1')
        self.run_turn(first)
        identity = self.record('apply-1')['record']['thread_id']
        with self.state.transaction() as db:
            db.execute("UPDATE wf_tasks SET status='waiting_for_user' WHERE id=?", (first.task_id,))
        with self.assertRaises(BrowserRecoverable): self.run_turn(second)
        self.state.confirm(first)
        self.run_turn(second)
        value = self.record('apply-1')
        self.assertNotEqual(identity, value['record']['thread_id'])
        self.assertEqual(identity, value['history'][0]['record']['thread_id'])

    def test_failed_unknown_submission_still_owns_its_slot(self):
        first, second = self.lease('apply-1'), self.lease('apply-1')
        self.run_turn(first)
        with self.state.transaction() as db:
            db.execute("UPDATE wf_tasks SET status='failed' WHERE id=?", (first.task_id,))
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE task_id=?", (first.task_id,))
        with self.assertRaises(BrowserRecoverable): self.run_turn(second)

    def test_existing_shared_generation_retains_identity_and_one_slot_affinity(self):
        first, second = self.state.lease(), self.state.lease()
        self.run_turn(first)
        self.run_turn(second)
        old = self.calls[-1]['thread_id']
        first.application_worker_id = 'apply-1'
        first.verify_pool_transport=lambda: None  # Fictional host verification.
        first.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        self.run_turn(first)
        self.assertEqual(old, self.calls[-1]['thread_id'])
        second.application_worker_id = 'apply-2'
        second.verify_pool_transport=lambda: None  # Fictional host verification.
        second.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        with self.assertRaises(BrowserRecoverable): self.run_turn(second)
        self.state.confirm(first)
        second.application_worker_id = 'apply-1'
        second.verify_pool_transport=lambda: None  # Fictional host verification.
        second.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        self.run_turn(second)
        self.assertEqual(old, self.calls[-1]['thread_id'])

    def test_existing_per_task_identity_retained(self):
        self.transport.browser_session_policy = 'per_task'
        lease = self.state.lease()
        self.run_turn(lease)
        old = json.loads((self.runs / '_browser_sessions' / (lease.task_id + '.json')).read_text())['thread_id']
        lease.application_worker_id = 'apply-3'
        lease.verify_pool_transport=lambda: None  # Fictional host verification.
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        self.run_turn(lease, 'linkedin_session')
        self.assertEqual(old, self.calls[-1]['thread_id'])

    def test_verified_prep_reset_creates_new_identity_and_preserves_old_record(self):
        lease = self.state.lease()
        self.run_turn(lease)
        shared_path = self.runs / '_browser_sessions' / '_shared.json'
        before = shared_path.read_bytes()
        old = json.loads(before)['generations'][0]['record']['thread_id']
        lease.application_worker_id = 'apply-2'
        lease.verify_pool_transport=lambda: None  # Fictional host verification.
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        lease.pool_session_reset = {'task_id': lease.task_id, 'worker_id': 'apply-2',
            'legacy_thread_id': old, 'safe_preparation_reset': True,
            'stopped_worker_confirmed': True, 'owned_tabs_closed': True,
            'evidence_reference': 'fictional/audited-safe-reset.json'}
        self.run_turn(lease)
        value = self.record('apply-2')
        self.assertIsNone(self.calls[-1]['thread_id'])
        self.assertNotEqual(old, value['record']['thread_id'])
        self.assertEqual(old, value['legacy_record']['thread_id'])
        self.assertEqual(before, shared_path.read_bytes())

    def test_tampered_receipt_and_unknown_reset_rejected(self):
        lease = self.lease('apply-1')
        self.run_turn(lease)
        record = self.record('apply-1')['record']
        path = self.runs / record['receipt'] / 'receipt.json'
        value = json.loads(path.read_text())
        value['application_worker_id'] = 'apply-2'
        _replace_private_json(path, value)
        with self.assertRaises(CodexTransportError): self.run_turn(lease)
        self.assertEqual(1, self.native.call_count)

    def test_active_legacy_binding_requires_exact_safe_host_reset(self):
        from pooled_browser_session import open_session
        self.transport.browser_session_policy='per_task'
        lease=self.state.lease()
        self.run_turn(lease)
        path=self.runs/'_browser_sessions'/(lease.task_id+'.json')
        old=json.loads(path.read_text());old['status']='active'
        _replace_private_json(path,old)
        original=path.read_bytes()
        lease.application_worker_id='apply-1'
        lease.verify_pool_transport=lambda: None
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        with self.assertRaises(BrowserWorkUncertain):open_session(self.transport,lease,'application_prepare')
        lease.pool_session_reset={'task_id':lease.task_id,'worker_id':'apply-1',
            'legacy_thread_id':str(uuid.uuid4()),'safe_preparation_reset':True,
            'stopped_worker_confirmed':True,'owned_tabs_closed':True,
            'evidence_reference':'fictional/stopped-exact-owner.json'}
        with self.assertRaises(ValueError):open_session(self.transport,lease,'application_prepare')
        lease.pool_session_reset['legacy_thread_id']=old['thread_id']
        with self.state.transaction() as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE task_id=?",(lease.task_id,))
        with self.assertRaises(BrowserWorkUncertain):open_session(self.transport,lease,'application_prepare')
        with self.state.transaction() as db:
            db.execute("UPDATE wf_processes SET submission_status='preparing' WHERE task_id=?",(lease.task_id,))
        self.run_turn(lease)
        self.assertEqual(original,path.read_bytes())
        self.assertEqual(old,self.record('apply-1')['legacy_record'])
        self.assertIsNone(self.calls[-1]['thread_id'])
        self.assertEqual(2,len(self.calls))

    def test_reset_requires_safe_known_submission_and_closed_tabs(self):
        lease = self.state.lease()
        self.run_turn(lease)
        shared_path = self.runs / '_browser_sessions' / '_shared.json'
        old = json.loads(shared_path.read_text())['generations'][0]['record']['thread_id']
        lease.application_worker_id = 'apply-2'
        lease.verify_pool_transport=lambda: None  # Fictional host verification.
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        lease.pool_session_reset = {'task_id': lease.task_id, 'worker_id': 'apply-2',
            'legacy_thread_id': old, 'safe_preparation_reset': True,
            'stopped_worker_confirmed': True, 'owned_tabs_closed': False,
            'evidence_reference': 'fictional/audited-reset.json'}
        with self.assertRaises(CodexTransportError): self.run_turn(lease)
        lease.pool_session_reset['owned_tabs_closed'] = True
        with self.state.transaction() as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE task_id=?", (lease.task_id,))
        with self.assertRaises(BrowserWorkUncertain): self.run_turn(lease)
        self.assertEqual(1, self.native.call_count)

    def test_completed_reset_does_not_wait_for_later_legacy_activity(self):
        lease = self.state.lease()
        self.run_turn(lease)
        shared_path = self.runs / '_browser_sessions' / '_shared.json'
        old = json.loads(shared_path.read_text())['generations'][0]['record']['thread_id']
        lease.application_worker_id = 'apply-2'
        lease.verify_pool_transport=lambda: None  # Fictional host verification.
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        lease.pool_session_reset = {'task_id': lease.task_id, 'worker_id': 'apply-2',
            'legacy_thread_id': old, 'safe_preparation_reset': True,
            'stopped_worker_confirmed': True, 'owned_tabs_closed': True,
            'evidence_reference': 'fictional/audited-reset.json'}
        self.run_turn(lease)
        own = self.record('apply-2')['record']['thread_id']
        shared = json.loads(shared_path.read_text())
        shared['generations'][0]['record']['status'] = 'active'
        _replace_private_json(shared_path, shared)
        self.run_turn(lease)
        self.assertEqual(own, self.calls[-1]['thread_id'])

    def test_legacy_session_only_keeps_original_browser_binding(self):
        lease = self.state.lease()
        self.run_turn(lease)
        lease.application_worker_id = 'apply-1'
        lease.verify_pool_transport=lambda: None  # Fictional host verification.
        lease.admit_application_tab=lambda role,thread_id: None  # Fictional host admission.
        self.run_turn(lease)
        shared_path = self.runs / '_browser_sessions' / '_shared.json'
        before = shared_path.read_bytes()
        lease.session_only_handoff_verified = True
        self.run_turn(lease, 'application_question_match', model=True)
        self.assertEqual(before, shared_path.read_bytes())
        self.run_turn(lease)
        self.assertEqual(self.record('apply-1')['record']['thread_id'], self.calls[-1]['thread_id'])


if __name__ == '__main__':
    unittest.main()
