"""Two fictional device clients against the real v1 HTTP boundary."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from question_pool import SharedQuestionPool
from server import ReviewServer
from test_store import catalog


class QuestionSyncAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.db=self.root/'questions.sqlite3';self.server=None;self.thread=None
        self.start_server()
        self.store.import_catalog(catalog(),'Fictional cross-device fixture')
        self.processes=[]
        for n in range(2):
            with self.store.transaction(True) as db:
                db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key=?",(f'a:{n+1}',))
                pid=self.store._create_application(db,self.store._row(db,'jobs','key',f'a:{n+1}'),f'fixture-{n}')
            task=self.store.lease_task('fixture',['apply'],application_worker_id=f'apply-{n+1}')
            self.store.start_application(pid,task['lease_token'])
            qid=f'q{n}'
            self.store.checkpoint_task(task['id'],task['lease_token'],{'question_bindings':{qid:'authorization'}})
            self.store.ask_questions(pid,task['lease_token'],f'form-{n}',[{
                'id':qid,'text':'Are you authorized to work in the United States?',
                'type':'single_choice','required':True,'choices':[
                    {'id':f'yes-{n}','label':'Yes'},{'id':f'no-{n}','label':'No'}]}])
            self.store.refresh_application_slot(f'apply-{n+1}')
            self.processes.append((pid,task['id'],qid))
        with self.pool.hold('worker','apply-1'):
            shared=self.pool.register_questions(self.processes[0][0],'apply-1',['q0'])['mappings']['q0']
            self.pool.register_questions(self.processes[1][0],'apply-2',['q1'],matches={'q1':shared})
        self.shared=shared

    def start_server(self):
        self.server=ReviewServer(('127.0.0.1',0),self.db,self.root)
        self.store=self.server.store
        self.pool=self.store.question_pool=SharedQuestionPool(self.store).initialize()
        self.format_calls=[]
        def format_answer(spec,payload):
            self.format_calls.append(payload['request_id']);return spec['append_text']
        self.store.answer_formatter=format_answer
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_port

    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)

    def tearDown(self):
        self.stop_server();self.temp.cleanup()

    def request(self,method,path,payload=None,device='Mac'):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            connection.request(method,path,body=json.dumps(payload) if payload is not None else None,
                headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{self.port}',
                         'X-Review-Token':self.server.token,'User-Agent':f'Fictional-{device}'})
            response=connection.getresponse();return response.status,json.loads(response.read())
        finally:connection.close()

    def read(self,device='Mac'):
        status,data=self.request('GET','/api/v1/questions',device=device)
        self.assertEqual(200,status,data);self.assertTrue(data['complete']);return data['questions']

    def command(self,index=0,device='Mac',value=None):
        question=next(q for q in self.read(device) if q['id']==f'q{index}')
        process=next(p for p in self.store.processes() if p['id']==question['process_id'])
        return {'request_id':f'{device}-{index}','process_id':question['process_id'],
            'process_revision':process['revision'],'form_version':question['form_version'],
            'answers':[{'question_id':question['id'],'question_revision':question['revision'],
                        'value':value or f'yes-{index}'}]}

    def assert_saved(self,device='iOS'):
        questions=self.read(device)
        self.assertEqual({self.shared},{q['pool_question_id'] for q in questions})
        self.assertTrue(all(type(q['pool_revision']) is int for q in questions))
        self.assertTrue(all(q['answered_at'] for q in questions))
        self.assertEqual({'q0':'yes-0','q1':'yes-1'},{q['id']:q['answer'] for q in questions})
        self.assertEqual({'ready'},{w['state'] for w in self.store.application_workflow()['workers'] if w['task_id']})

    def test_mac_answer_is_visible_on_ios_with_option_mapping_and_legacy_receipt(self):
        status,receipt=self.request('POST','/api/v1/answers',self.command(),device='Mac')
        self.assertEqual(200,status,receipt)
        self.assertEqual([self.processes[0][0]],receipt['process_ids'])
        self.assertEqual([self.processes[0][1]],receipt['task_ids'])
        self.assertEqual({p[0] for p in self.processes},set(receipt['affected_process_ids']))
        self.assert_saved('iOS')

    def test_ios_answer_is_visible_on_mac(self):
        status,receipt=self.request('POST','/api/v1/answers',self.command(1,'iOS'),device='iOS')
        self.assertEqual(200,status,receipt);self.assert_saved('Mac')

    def test_identical_simultaneous_device_answers_are_idempotent(self):
        commands=[self.command(0,'Mac'),self.command(1,'iOS')]
        barrier=threading.Barrier(2)
        def send(command):
            barrier.wait();return self.request('POST','/api/v1/answers',command)
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(send,commands))
        self.assertEqual([200,200],[r[0] for r in results],results)
        self.assertTrue(any(r[1]['already_answered'] for r in results))
        with self.store.transaction() as db:
            self.assertEqual(1,db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0])
        self.assert_saved()

    def test_conflicting_stale_device_cannot_replace_saved_answer(self):
        stale=self.command(1,'iOS','no-1')
        self.assertEqual(200,self.request('POST','/api/v1/answers',self.command())[0])
        status,error=self.request('POST','/api/v1/answers',stale,device='iOS')
        self.assertEqual(409,status,error);self.assert_saved()

    def test_equivalent_stale_retry_avoids_formatter_and_survives_restart(self):
        stale=self.command(1,'iOS')
        self.assertEqual(200,self.request('POST','/api/v1/answers',self.command())[0])
        self.stop_server();self.start_server()
        status,receipt=self.request('POST','/api/v1/answers',stale,device='iOS')
        self.assertEqual(200,status,receipt);self.assertTrue(receipt['already_answered'])
        self.assertEqual([],self.format_calls);self.assert_saved()
        for i in (1,2):
            task=self.store.lease_task('restart',['apply'],application_worker_id=f'apply-{i}')
            self.assertEqual(self.processes[i-1][1],task['id'])

    def test_same_request_id_conflict_rejected_and_duplicate_safe(self):
        payload=self.command();self.assertEqual(200,self.request('POST','/api/v1/answers',payload)[0])
        self.assertEqual(200,self.request('POST','/api/v1/answers',payload)[0])
        conflict=copy.deepcopy(payload);conflict['answers'][0]['value']='no-0'
        self.assertEqual(409,self.request('POST','/api/v1/answers',conflict)[0]);self.assert_saved()

    def test_read_waits_until_fanout_finishes(self):
        entered,release=threading.Event(),threading.Event()
        original=self.store._answer_questions
        def paused(payload,**kwargs):
            if payload['process_id']==self.processes[1][0]:
                entered.set();release.wait(2)
            return original(payload,**kwargs)
        self.store._answer_questions=paused
        payload=self.command()
        with ThreadPoolExecutor(max_workers=2) as executor:
            answer=executor.submit(self.request,'POST','/api/v1/answers',payload)
            self.assertTrue(entered.wait(1))
            read=executor.submit(self.request,'GET','/api/v1/questions')
            self.assertFalse(read.done());release.set()
            self.assertEqual(200,answer.result()[0]);status,data=read.result()
        self.assertEqual(200,status);self.assertTrue(all(q['answer'] is not None for q in data['questions']))

    def test_workflow_endpoint_matches_contract_and_omits_answers(self):
        status,data=self.request('GET','/api/v1/application-workflow')
        self.assertEqual(200,status);workflow=data['application_workflow']
        self.assertEqual(3,workflow['pool_size']);self.assertEqual(3,len(workflow['workers']))
        self.assertEqual({'apply-1','apply-2','apply-3'},{w['worker_id'] for w in workflow['workers']})
        self.assertNotIn('Are you authorized',json.dumps(workflow))
        self.assertNotIn(str(self.root),json.dumps(workflow))
        self.assertTrue(self.request('GET','/api/v1/capabilities')[1]['capabilities']['application_workflow'])

    def test_crash_after_source_commit_recovers_fanout_before_read_or_dispatch(self):
        payload=self.command()
        specification=self.pool.formatting_spec(payload)
        with self.pool.hold('answer_handler','crashed-device'):
            self.pool.save_answered_questions(specification['append_text'])
            self.assertIsNone(self.store.lease_task('too-early',['apply'],application_worker_id='apply-1'))
        self.stop_server();self.start_server()
        self.assert_saved()
        self.assertEqual([],self.format_calls)

    def test_partial_shared_answer_keeps_only_remaining_application_paused(self):
        with self.store.transaction(True) as db:
            db.execute("""INSERT INTO wf_questions
                SELECT 'extra',process_id,form_version,1,'Fictional start date','text','[]',1,
                       scope,reason,NULL,NULL,0,created_at,updated_at FROM wf_questions WHERE id='q1'""")
        with self.pool.hold('worker','apply-2'):
            self.pool.register_questions(self.processes[1][0],'apply-2',['extra'])
        self.assertEqual(200,self.request('POST','/api/v1/answers',self.command())[0])
        workers={w['worker_id']:w for w in self.store.application_workflow()['workers']}
        self.assertEqual('ready',workers['apply-1']['state'])
        self.assertEqual('paused',workers['apply-2']['state'])
        question=next(q for q in self.read('iOS') if q['id']=='extra')
        process=next(p for p in self.store.processes() if p['id']==question['process_id'])
        payload={'request_id':'remaining-ios','process_id':process['id'],'process_revision':process['revision'],
            'form_version':question['form_version'],'answers':[{'question_id':'extra',
                'question_revision':question['revision'],'value':'Next month'}]}
        self.assertEqual(200,self.request('POST','/api/v1/answers',payload,device='iOS')[0])
        self.assertEqual('ready',self.store.application_workflow()['workers'][1]['state'])

if __name__=='__main__':unittest.main()
