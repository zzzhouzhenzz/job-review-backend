"""Fictional host ownership checks. No native browser or employer actions."""
import json
import hashlib
import uuid
from pathlib import Path
from unittest.mock import patch
import unittest

import test_application_pool as pool_fixtures
import test_codex_runtime as runtime_fixtures
from orchestrator import TaskLease
from codex_transport import _private_write
from store import ReviewError
from workflow import WorkflowStore


def opening_receipt(root, task, tab='fictional-tab', thread=None):
    run=Path(root)/str(uuid.uuid4());run.mkdir(mode=0o700)
    result={'outcome':'opened','tab_id':tab,'job_url':task['job_url'],
        'created_new':True,'handoff_preserved':True,'blocker':'','blocker_reason':''}
    receipt={'role':'application_tab_open','status':'completed','transport':'app-server',
        'task_id':task['id'],'application_worker_id':task['checkpoint']['application_worker_id'],
        'turn_completed':True,'browser_verified':True,'thread_id':thread or str(uuid.uuid4()),
        'lease_digest':hashlib.sha256(task['lease_token'].encode()).hexdigest(),
        'browser':True,'session_worker':False,'exit_code':0,
        'turn_id':str(uuid.uuid4())}
    _private_write(run/'receipt.json',json.dumps(receipt));_private_write(run/'result.json',json.dumps(result))
    return result,run/'receipt.json'


class ApplicationTabAffinityTests(unittest.TestCase):
    setUp=pool_fixtures.ApplicationPoolTests.setUp
    tearDown=pool_fixtures.ApplicationPoolTests.tearDown

    def task(self, worker='apply-1'):
        task=self.store.lease_task('fictional',['apply'],application_worker_id=worker)
        process=self.store.start_application(task['payload']['process_id'],task['lease_token'])
        task['job_url']=process['snapshot']['url']
        return task

    def bind(self,task,tab='fictional-tab'):
        result,path=opening_receipt(self.tmp.name,task,tab)
        return self.store.bind_application_tab(task['id'],task['lease_token'],
            task['checkpoint']['application_worker_id'],result,receipt_path=path)

    def test_exact_binding_survives_restart_and_rejects_foreign_tab_thread_worker(self):
        task=self.task();binding=self.bind(task)
        restarted=WorkflowStore(self.store.path,low_watermark=0)
        read=lambda **kw:restarted.application_tab_binding(task['id'],task['lease_token'],'apply-1',**kw)
        self.assertEqual(binding['id'],read(required=True)['id'])
        for kw in ({'requested_tab_id':'another-tab'},{'native_thread_id':str(uuid.uuid4())},
                   {'expected_binding_id':'obsolete-generation'}):
            with self.subTest(kw=kw),self.assertRaises(ReviewError):read(**kw)
        with self.assertRaises(ReviewError):
            restarted.application_tab_binding(task['id'],task['lease_token'],'apply-2')
        with self.assertRaises(ReviewError):TaskLease(restarted,task).admit_application_tab('application_prepare',None)

    def test_binding_cannot_be_adopted_by_another_task_or_replaced(self):
        first=self.task();self.bind(first)
        second=self.task('apply-2')
        with self.assertRaises(ReviewError):self.bind(second)
        with self.assertRaises(ReviewError):self.bind(first,'replacement-tab')
        with self.assertRaises(ReviewError):
            self.store.checkpoint_task(first['id'],first['lease_token'],{'application_tab_binding':{'tab_id':'fake'}})

    def test_stale_lease_or_material_cannot_admit_native_work(self):
        task=self.task();self.bind(task)
        with self.assertRaises(ReviewError):
            self.store.application_tab_binding(task['id'],'stale-token','apply-1')
        with self.store.transaction(True) as db:
            row=db.execute('SELECT snapshot FROM wf_processes WHERE task_id=?',(task['id'],)).fetchone()
            snapshot=json.loads(row['snapshot']);snapshot['material_hash']='changed'
            db.execute('UPDATE wf_processes SET snapshot=? WHERE task_id=?',(json.dumps(snapshot),task['id']))
        with self.assertRaises(ReviewError):TaskLease(self.store,task).admit_application_tab('application_prepare',str(uuid.uuid4()))

    def test_paused_application_keeps_slot_and_binding_until_same_task_resumes(self):
        task=self.task('apply-3');binding=self.bind(task)
        process=self.store.ask_questions(task['payload']['process_id'],task['lease_token'],'form',
            [{'id':'q','text':'Fictional missing answer','type':'text','required':True}])
        self.assertIsNone(self.store.lease_task('other',['apply'],application_worker_id='apply-3'))
        self.assertNotEqual(task['id'],self.task('apply-1')['id'])
        q=self.store.questions()[0]
        self.store.answer_questions({'request_id':'answer','process_id':process['id'],
            'process_revision':process['revision'],'form_version':'form',
            'answers':[{'question_id':q['id'],'question_revision':q['revision'],'value':'Saved'}]})
        resumed=self.store.lease_task('same',['apply'],application_worker_id='apply-3')
        self.assertEqual(task['id'],resumed['id'])
        self.assertEqual(binding['id'],TaskLease(self.store,resumed).admit_application_tab(
            'application_prepare',binding['native_thread_id'])['id'])

    def test_bootstrap_rejects_result_drift_and_missing_native_evidence(self):
        task=self.task();result,path=opening_receipt(self.tmp.name,task)
        for field,value in (('tab_id','foreign'),('created_new',False),('job_url','https://example.com/other')):
            with self.subTest(field=field),self.assertRaises(ReviewError):
                self.store.bind_application_tab(task['id'],task['lease_token'],'apply-1',{**result,field:value},receipt_path=path)
        self.assertIsNone(self.store.application_tab_binding(task['id'],task['lease_token'],'apply-1'))
        with self.assertRaises(ReviewError):TaskLease(self.store,task).admit_application_tab('application_prepare',None)

    def test_old_lease_or_incomplete_receipt_cannot_bind(self):
        task=self.task();result,path=opening_receipt(self.tmp.name,task)
        original=json.loads(path.read_text())
        for key,value in (('lease_digest','old'),('browser',False),('exit_code',1),('exit_code',False),
                          ('session_worker',True)):
            with self.subTest(key=key):
                path.write_text(json.dumps({**original,key:value}))
                with self.assertRaises(ReviewError):
                    self.store.bind_application_tab(task['id'],task['lease_token'],'apply-1',result,receipt_path=path)


class ApplicationTabRuntimeTests(unittest.TestCase):
    setUp=runtime_fixtures.CodexRuntimeTests.setUp
    runtime=runtime_fixtures.CodexRuntimeTests.runtime
    prepared=runtime_fixtures.CodexRuntimeTests.prepared

    def task(self):
        card=self.store.feed()['cards'][0]
        self.store.receive_decision({'request_id':'fixture','action':'apply','card_id':card['id'],
            'card_revision':card['revision'],'selected':[card['jobs'][0]['key']]})
        task=self.store.lease_task('test',['apply'],application_worker_id='apply-1')
        task['browser']=runtime_fixtures.BROWSER
        process=self.store.start_application(task['payload']['process_id'],task['lease_token'])
        task['job_url']=process['snapshot']['url']
        return task

    def test_open_commits_binding_before_prepare_and_foreign_result_is_not_adopted(self):
        task=self.task();runtime=self.runtime([]);calls=[]
        def run(role,prompt,schema,lease,**kwargs):
            calls.append(role)
            if role=='application_tab_open':
                result,path=opening_receipt(self.root,task)
                lease._last_native_browser_receipt=path
                return result
            self.assertIsNotNone(self.store.application_tab_binding(task['id'],lease.token,'apply-1'))
            self.assertIn('fictional-tab',prompt)
            return {**self.prepared(),'tab_id':'foreign-tab'}
        runtime.transport.run=run
        with self.assertRaises(ReviewError):runtime.apply(self.store,task)
        self.assertEqual(['application_tab_open','application_prepare'],calls)
        binding=self.store.application_tab_binding(task['id'],task['lease_token'],'apply-1')
        self.assertEqual('fictional-tab',binding['tab_id'])
        with self.store.transaction() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM wf_submission_attempts').fetchone()[0])


if __name__=='__main__':unittest.main()
