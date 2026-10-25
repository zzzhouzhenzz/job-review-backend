"""Fictional tests for independently leased durable application slots."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from workflow import WorkflowStore
from orchestrator import Orchestrator
from worker_runner import WorkerRunner
from test_store import catalog
from test_readiness import ReadyRuntime
from store import ReviewError
from unittest.mock import patch
from orchestrator import TaskLease
from codex_transport import SessionOnlyHandoffUnverified
import json


class ApplicationPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=WorkflowStore(Path(self.tmp.name)/'pool.sqlite3',low_watermark=0)
        self.store.import_catalog(catalog(),'Fictional application pool')
        self.store.grant_project_consent({'request_id':'consent','policy_version':1,'accepted':True})
        card=next(c for c in self.store.feed()['cards'] if c['company']['id']=='a')
        result=self.store.receive_decision({'request_id':'all-alpha','action':'apply','card_id':card['id'],
            'card_revision':card['revision'],'selected':[j['key'] for j in card['jobs']]})
        self.tasks=result['task_ids']

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_leases_have_distinct_task_and_resource_and_survive_restart(self):
        def claim(n):
            return self.store.lease_task('owner',kinds=['apply'],application_worker_id=f'apply-{n}')
        with ThreadPoolExecutor(max_workers=3) as executor:
            tasks=list(executor.map(claim,(1,2,3)))
        self.assertEqual(3,len({t['id'] for t in tasks}))
        self.assertEqual({'browser:apply-1','browser:apply-2','browser:apply-3'},{t['resource'] for t in tasks})
        restarted=WorkflowStore(self.store.path,low_watermark=0)
        self.assertEqual({t['id'] for t in tasks},{w['task_id'] for w in restarted.application_workflow()['workers']})
        for n in (1,2,3):
            self.assertIsNone(restarted.lease_task('other',kinds=['apply'],application_worker_id=f'apply-{n}'))

    def test_paused_slot_retains_task_and_answer_resumes_same_slot(self):
        task=self.store.lease_task('owner',kinds=['apply'],application_worker_id='apply-2')
        pid=task['payload']['process_id'];self.store.start_application(pid,task['lease_token'])
        process=self.store.ask_questions(pid,task['lease_token'],'form',[{'id':'missing','text':'Fictional answer',
            'type':'text','required':True}])
        self.store.refresh_application_slot('apply-2')
        self.assertIsNone(self.store.lease_task('owner',kinds=['apply'],application_worker_id='apply-2'))
        other=self.store.lease_task('owner',kinds=['apply'],application_worker_id='apply-1')
        self.assertNotEqual(task['id'],other['id'])
        q=self.store.questions()[0]
        self.store.answer_questions({'request_id':'answer','process_id':pid,'process_revision':process['revision'],
            'form_version':'form','answers':[{'question_id':q['id'],'question_revision':q['revision'],'value':'Confirmed'}]})
        resumed=self.store.lease_task('owner',kinds=['apply'],application_worker_id='apply-2')
        self.assertEqual(task['id'],resumed['id'])
        self.assertEqual('apply-2',resumed['checkpoint']['application_worker_id'])

    def test_legacy_global_quarantine_cannot_be_hidden_by_slot_assignment(self):
        task=self.store.lease_task('legacy',kinds=['apply'])
        self.store.quarantine_task(task['id'],task['lease_token'],'browser_work_uncertain')
        self.assertIsNone(self.store.lease_task('pool',kinds=['apply'],application_worker_id='apply-1'))
        self.assertTrue(next(t for t in self.store.tasks() if t['id']==task['id'])['resource_quarantined'])

    def test_worker_cannot_introduce_or_change_host_pool_context(self):
        task=self.store.lease_task('owner',['apply'])
        for field,value in (('application_worker_id','apply-1'),('pool_session_reset',{'safe_preparation_reset':True}),
                            ('pool_transport_proof',{'session_only_handoff_preserved':True})):
            for merge in (False,True):
                with self.subTest(field=field,merge=merge),self.assertRaises(ReviewError):
                    self.store.checkpoint_task(task['id'],task['lease_token'],{field:value},merge=merge)
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?',
                (json.dumps({'application_worker_id':'apply-1','pool_session_reset':{'host':True},
                    'pool_transport_proof':{'host':True}}),task['id']))
        for field,value in (('application_worker_id','apply-2'),('pool_session_reset',{}),('pool_transport_proof',{}),
                            ('pool_session_reset',{'host':1}),('pool_transport_proof',{'host':1})):
            with self.subTest(field=field),self.assertRaises(ReviewError):
                self.store.checkpoint_task(task['id'],task['lease_token'],{field:value},merge=True)
        saved=self.store.checkpoint_task(task['id'],task['lease_token'],{'safe_progress':'read form'})
        self.assertEqual({'host':True},saved['checkpoint']['pool_transport_proof'])

    def test_host_assignment_requires_current_proof_and_seeds_future_tasks(self):
        self.store.application_pool_proof_required=True
        with patch('application_pool.current_transport_proof',return_value=None):
            self.assertIsNone(self.store.lease_task('owner',['apply'],application_worker_id='apply-1'))
        marker={'evidence_reference':'fictional-proof','deployment_identity_digest':'a'*64}
        with patch('application_pool.current_transport_proof',return_value=marker):
            task=self.store.lease_task('owner',['apply'],application_worker_id='apply-1')
            self.assertEqual(marker,task['checkpoint']['pool_transport_proof'])
            lease=TaskLease(self.store,task)
            self.assertTrue(lease.session_only_handoff_verified)
            following=self.store.lease_task('next-owner',['apply'],application_worker_id='apply-2')
            self.assertNotEqual(task['id'],following['id'])
            self.assertEqual(marker,following['checkpoint']['pool_transport_proof'])
        with patch('application_pool.current_transport_proof',return_value=None):
            self.assertFalse(lease.session_only_handoff_verified)
            with self.assertRaises(SessionOnlyHandoffUnverified):lease.verify_pool_transport()

    def test_three_runtime_calls_overlap_and_pausing_one_does_not_stop_others(self):
        barrier=threading.Barrier(3);release=threading.Event();paused=threading.Event();entered=set();guard=threading.Lock()
        class PoolRuntime(ReadyRuntime):
            application_pool_size=3
            supported_kinds={'apply'}
            def apply(runtime,store,task):
                worker=task['checkpoint']['application_worker_id']
                with guard: entered.add(worker)
                barrier.wait(3)
                if worker != 'apply-2':
                    if not release.wait(3): raise RuntimeError('Fictional release timed out')
                store.ask_questions(task['payload']['process_id'],task['lease_token'],'form-'+worker,
                    [{'id':'question-'+worker,'text':'Fictional question','type':'text','required':True}])
                if worker=='apply-2': paused.set()
        orch=Orchestrator(self.store,PoolRuntime());orch.readiness.start();orch.readiness.wait(2)
        runner=WorkerRunner(orch,poll_interval=.01)
        try:
            runner.start();self.assertTrue(paused.wait(3));self.assertEqual(3,len(entered))
            statuses=[t['status'] for t in self.store.tasks() if t['kind']=='apply']
            self.assertEqual(2,statuses.count('running'));self.assertEqual(1,statuses.count('waiting_for_user'))
        finally:
            release.set();runner.stop(timeout=4);orch.readiness.close()

if __name__=='__main__': unittest.main()
