"""Shared-session lifecycle against private fictional state; no employer calls."""
import contextlib
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from codex_appserver import AppServerError, AppServerResult, archive_worker_thread
from codex_transport import CodexTransport, CodexWorkerStopped, _replace_private_json
from readiness import BrowserWorkUncertain, WorkerCapacityUnavailable
from shared_browser_session import (REGISTRY, initial_registry, open_session, read_registry,
                                    successful_applications, verified_record)

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
          'required': ['ok'], 'additionalProperties': False}


class State:
    def __init__(self, path):
        self.path = path
        with self.transaction() as db:
            db.executescript('''CREATE TABLE wf_tasks(id TEXT PRIMARY KEY, kind TEXT, status TEXT);
                CREATE TABLE wf_processes(id TEXT PRIMARY KEY,task_id TEXT,submission_status TEXT,
                    receipt TEXT,snapshot TEXT);
                CREATE TABLE wf_submission_attempts(process_id TEXT,task_id TEXT,confirmed_at TEXT,receipt TEXT);''')

    @contextlib.contextmanager
    def transaction(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            yield db

    def lease(self, kind='apply', status='running'):
        task_id = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute('INSERT INTO wf_tasks VALUES (?,?,?)', (task_id, kind, status))
            if kind == 'apply':
                db.execute('INSERT INTO wf_processes VALUES (?,?,?,?,?)',
                    (task_id, task_id, 'preparing', None, json.dumps({'job_id': task_id, 'url': 'https://example.com/' + task_id})))
        return SimpleNamespace(task_id=task_id, token=str(uuid.uuid4()), store=self, heartbeat=lambda: True)

    def confirm(self, lease, copies=1):
        with self.transaction() as db:
            db.execute("UPDATE wf_tasks SET status='succeeded' WHERE id=?", (lease.task_id,))
            db.execute("UPDATE wf_processes SET submission_status='submitted',receipt='confirmed' WHERE id=?", (lease.task_id,))
            for _ in range(copies):
                db.execute('INSERT INTO wf_submission_attempts VALUES (?,?,?,?)',
                    (lease.task_id, lease.task_id, '2026-10-19', 'confirmed'))


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.state = State(self.root / 'fixture.sqlite')
        self.calls = []
        self.transport = CodexTransport(Path('/usr/bin/true'), self.runs, cwd=self.root,
            browser_transport='app-server', browser_session_policy='shared')
        self.addCleanup(self.transport.close)
        self.native = patch('codex_appserver.run_appserver', side_effect=self.result).start()
        self.addCleanup(patch.stopall)
        self.archive = patch('codex_appserver.archive_worker_thread', return_value={'archived': True}).start()

    def result(self, **kwargs):
        self.calls.append(kwargs)
        return AppServerResult({'ok': True}, kwargs['thread_id'] or str(uuid.uuid4()),
            True, True, (('cua_repl', 'js'),), 100, 0, turn_id=str(uuid.uuid4()))

    def run_turn(self, lease, role='application_prepare'):
        return self.transport.run(role, 'Current exact job data.', SCHEMA, lease, browser=True)

    def registry(self):
        return read_registry(self.runs / '_browser_sessions' / REGISTRY)

    def test_readiness_work_multiple_jobs_and_restart_reuse_one_identity(self):
        first, second = self.state.lease(), self.state.lease('explore')
        for lease, role in ((first, 'linkedin_session'), (first, 'application_prepare'), (second, 'explorer')):
            self.run_turn(lease, role)
        identity = self.registry()['generations'][0]['record']['thread_id']
        self.assertEqual([None, identity, identity], [c['thread_id'] for c in self.calls])
        self.transport = CodexTransport(Path('/usr/bin/true'), self.runs, cwd=self.root,
            browser_transport='app-server', browser_session_policy='shared')
        self.addCleanup(self.transport.close)
        self.run_turn(first)
        self.assertEqual(identity, self.calls[-1]['thread_id'])
        self.assertEqual({'Job Review worker 1'}, {c['thread_name'] for c in self.calls})
        self.assertFalse(list((self.runs / '_browser_sessions').glob('*.readiness.json')))

    def test_only_distinct_confirmed_matching_receipts_count(self):
        confirmed, unknown, partial = [self.state.lease() for _ in range(3)]
        self.state.confirm(confirmed, copies=2)
        with self.state.transaction() as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown',receipt='maybe' WHERE id=?", (unknown.task_id,))
            db.execute("UPDATE wf_processes SET submission_status='submitted',receipt='confirmed' WHERE id=?", (partial.task_id,))
            db.execute('INSERT INTO wf_submission_attempts VALUES (?,?,?,?)', (partial.task_id, partial.task_id, 'today', 'different'))
        self.assertEqual(1, successful_applications(self.state, [x.task_id for x in (confirmed, unknown, partial)]))

    def test_rotate_after_ten_and_archive_old_before_eleventh_work(self):
        for _ in range(10):
            lease = self.state.lease()
            self.run_turn(lease)
            self.state.confirm(lease)
        identity = self.registry()['generations'][0]['record']['thread_id']
        self.assertEqual(1, sum(c['thread_id'] is None for c in self.calls))
        self.archive.assert_not_called()
        self.run_turn(self.state.lease())
        registry = self.registry()
        self.assertEqual(2, registry['current'])
        self.assertTrue(registry['generations'][0]['archived'])
        self.assertEqual(10, registry['generations'][0]['confirmed_applications'])
        self.assertNotEqual(identity, registry['generations'][1]['record']['thread_id'])
        self.assertEqual(identity, self.archive.call_args.args[2])

    def test_pending_draft_stays_on_old_generation_then_archives_when_finished(self):
        pending = self.state.lease()
        self.run_turn(pending)
        for _ in range(10):
            done = self.state.lease()
            self.run_turn(done)
            self.state.confirm(done)
        old = self.registry()['generations'][0]['record']['thread_id']
        self.run_turn(self.state.lease('explore'), 'explorer')
        self.archive.assert_not_called()
        self.run_turn(pending)
        self.assertEqual(old, self.calls[-1]['thread_id'])
        self.state.confirm(pending)
        self.run_turn(self.state.lease('explore'), 'explorer')
        self.assertEqual(old, self.archive.call_args.args[2])

    def test_unknown_submission_even_on_failed_task_prevents_archival(self):
        pending = self.state.lease()
        self.run_turn(pending)
        with self.state.transaction() as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE id=?", (pending.task_id,))
            db.execute("UPDATE wf_tasks SET status='failed' WHERE id=?", (pending.task_id,))
        for _ in range(10):
            done = self.state.lease(); self.run_turn(done); self.state.confirm(done)
        self.run_turn(self.state.lease('explore'), 'explorer')
        self.archive.assert_not_called()

    def test_explorer_moves_after_rotation_without_losing_historical_receipt(self):
        explorer = self.state.lease('explore')
        for _ in range(10):
            done = self.state.lease(); self.run_turn(done); self.state.confirm(done)
        # Make explorer the final turn in the old generation before counting 10.
        registry = self.registry()
        registry['generations'][0]['tasks'].append(explorer.task_id)
        _replace_private_json(self.runs / '_browser_sessions' / REGISTRY, registry)
        self.run_turn(explorer, 'explorer')
        self.assertEqual(2, self.registry()['current'])
        self.assertIn(explorer.task_id, self.registry()['generations'][1]['tasks'])

    def test_failed_archive_keeps_identity_and_backs_off(self):
        for _ in range(10):
            done = self.state.lease(); self.run_turn(done); self.state.confirm(done)
        self.archive.side_effect = ValueError('changed native turn')
        self.run_turn(self.state.lease('explore'), 'explorer')
        self.assertFalse(self.registry()['generations'][0]['archived'])
        self.run_turn(self.state.lease('explore'), 'explorer')
        self.assertEqual(1, self.archive.call_count)

    def test_safe_first_capacity_failure_preserves_allocated_identity(self):
        identity = str(uuid.uuid4())
        self.native.side_effect = AppServerError('capacity', capacity=True, thread_id=identity)
        lease = self.state.lease()
        with self.assertRaises(WorkerCapacityUnavailable):
            self.run_turn(lease)
        self.assertEqual(identity, self.registry()['generations'][0]['record']['thread_id'])
        self.native.side_effect = self.result
        self.run_turn(lease)
        self.assertEqual(identity, self.calls[-1]['thread_id'])

    def test_uncertain_turn_blocks_every_job_without_starting_another_thread(self):
        self.native.side_effect = AppServerError('uncertain', uncertain=True, thread_id=str(uuid.uuid4()))
        original = self.state.lease()
        with self.assertRaises(BrowserWorkUncertain): self.run_turn(original)
        gate = self.transport.chrome_access_gate
        self.assertEqual('uncertain', gate.snapshot()['state'])
        self.assertEqual(original.task_id, gate.snapshot()['owner']['task_id'])
        self.native.reset_mock()
        waiting = self.state.lease('explore')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.run_turn, waiting, 'explorer')
            try:
                deadline = time.monotonic() + 2
                while not gate.snapshot()['queue'] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual([waiting.task_id], [item['task_id'] for item in gate.snapshot()['queue']])
                self.assertFalse(future.done())
                self.native.assert_not_called()
            finally:
                self.transport.close()
            with self.assertRaises(CodexWorkerStopped):
                future.result(timeout=2)
        self.native.assert_not_called()
        self.assertEqual([], gate.snapshot()['queue'])
        self.assertEqual('uncertain', gate.snapshot()['state'])
        self.assertEqual(original.task_id, gate.snapshot()['owner']['task_id'])

    def test_global_lock_excludes_concurrent_browser_calls(self):
        lease = self.state.lease()
        session = open_session(self.transport, lease, 'linkedin_session')
        try:
            with self.assertRaises(BlockingIOError): open_session(self.transport, self.state.lease(), 'explorer')
        finally:
            session.close()

    def test_existing_application_binding_is_reused_without_new_readiness_chat(self):
        legacy = CodexTransport(Path('/usr/bin/true'), self.runs, cwd=self.root, browser_transport='app-server')
        lease = self.state.lease()
        legacy.run('application_prepare', 'exact job', SCHEMA, lease, browser=True)
        identity = json.loads((self.runs / '_browser_sessions' / (lease.task_id + '.json')).read_text())['thread_id']
        self.run_turn(lease, 'linkedin_session')
        self.assertEqual(identity, self.calls[-1]['thread_id'])
        self.assertIsNone(self.calls[-1]['thread_name'])
        self.assertFalse((self.runs / '_browser_sessions' / (lease.task_id + '.readiness.json')).exists())

    def test_every_shared_turn_preserves_all_pending_owned_drafts(self):
        pending, other = self.state.lease(), self.state.lease()
        self.run_turn(pending)
        self.run_turn(other, 'linkedin_session')
        prompt = self.calls[-1]['prompt']
        self.assertIn(pending.task_id, prompt)
        self.assertIn(other.task_id, prompt)
        self.assertIn('markHandoff()', prompt)
        self.assertIn('ALREADY OWNED', prompt)
        self.assertIn('including health checks and exploration', prompt)
        saved = next(self.runs.glob('linkedin_session-*/prompt.txt')).read_text()
        self.assertEqual(prompt, saved)

    def test_registry_rejects_duplicate_native_identities(self):
        lease = self.state.lease(); self.run_turn(lease)
        registry = self.registry()
        other = dict(registry['generations'][0], number=2)
        registry['generations'].append(other)
        _replace_private_json(self.runs / '_browser_sessions' / REGISTRY, registry)
        with self.assertRaises(ValueError): self.registry()


LIFECYCLE_FIXTURE = r'''
import json,pathlib,sys
root=pathlib.Path.cwd(); mode=(root/'mode').read_text(); count=0
thread='11111111-1111-4111-8111-111111111111'; turn='22222222-2222-4222-8222-222222222222'
for line in sys.stdin:
 r=json.loads(line); m=r['method']
 with (root/'methods').open('a') as f: f.write(m+'\n')
 if m=='initialized': continue
 result={}
 if m=='thread/read': result={'thread':{'id':thread,'status':{'type':'active' if mode=='active' else 'notLoaded'}}}
 elif m=='thread/turns/list':
  count+=1
  result={'data':[{'id':('33333333-3333-4333-8333-333333333333' if mode=='changed' and count==2 else turn),
   'status':'inProgress' if mode=='pending' else 'completed','completedAt':100,
   'itemsView':'summary' if mode=='summary' else 'full',
   'items':[{'id':'final','type':'agentMessage','text':json.dumps({'ok':mode!='different'})}]}]}
 elif m=='thread/list': result={'data':[],'nextCursor':None}
 elif m=='thread/archive':
  if mode=='denied':
   print(json.dumps({'id':r['id'],'error':{'code':-32600,'message':'busy'}}),flush=True); continue
  print(json.dumps({'method':'thread/archived','params':{'threadId':thread}}),flush=True)
 elif m!='initialize': raise RuntimeError('unexpected mutation: '+m)
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
'''


class ArchiveWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'codex-fixture'
        self.binary.write_text('#!' + sys.executable + '\n' + LIFECYCLE_FIXTURE); self.binary.chmod(0o700)
        self.name = 'application_submit-' + str(uuid.uuid4())
        self.run = self.root / self.name; self.run.mkdir(mode=0o700)
        self.identity = '11111111-1111-4111-8111-111111111111'
        self.receipt = {'thread_id':self.identity,'transport':'app-server','status':'completed',
            'browser':True,'browser_verified':True,'turn_completed':True,'exit_code':0,
            'turn_id':'22222222-2222-4222-8222-222222222222'}
        _replace_private_json(self.run/'receipt.json',self.receipt)
        _replace_private_json(self.run/'result.json',{'ok':True})

    def call(self, mode='good', archive=True):
        (self.root/'mode').write_text(mode)
        return archive_worker_thread(self.binary,self.root,self.identity,receipt=self.receipt,
            run_directory=self.root,receipt_name=self.name,closed=threading.Event(),archive=archive,timeout=5)

    def test_archive_uses_only_passive_checks_then_exact_archive(self):
        result=self.call()
        self.assertTrue(result['archived'])
        methods=(self.root/'methods').read_text().splitlines()
        self.assertEqual(2,methods.count('thread/read'))
        self.assertEqual('thread/archive',methods[-1])
        self.assertFalse({'thread/start','thread/resume','turn/start'} & set(methods))

    def test_adoption_can_verify_without_any_mutation(self):
        self.assertFalse(self.call(archive=False)['archived'])
        self.assertNotIn('thread/archive',(self.root/'methods').read_text())

    def test_changed_active_partial_or_wrong_result_never_archives(self):
        for mode in ('active','pending','changed','summary','different'):
            with self.subTest(mode=mode):
                (self.root/'methods').write_text('')
                with self.assertRaises((ValueError,AppServerError)): self.call(mode)
                self.assertNotIn('thread/archive',(self.root/'methods').read_text())

    def test_native_archive_failure_is_not_reported_as_success(self):
        with self.assertRaises(AppServerError): self.call('denied')


if __name__ == '__main__': unittest.main()
