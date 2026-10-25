"""Durable ownership and observed stages for three independent application slots.

This module owns scheduling metadata only. Submission and authorization remain in
ApplicationMixin. A paused slot keeps its original task until it is terminal.
"""
from __future__ import annotations
import json
import hashlib
import uuid
from pathlib import Path

from store import ReviewError, now, encode, canonical_url, text

POOL_SIZE = 3
WORKER_IDS = tuple(f'apply-{n}' for n in range(1, POOL_SIZE + 1))
STATES = {'available','running','paused','ready','stopped','error'}
STAGES = {'idle','loading_answers','applying','waiting_for_lock','checking_hash',
          'refreshing_answers','matching_questions','saving_questions','paused',
          'ready','submitting','reconciling','completed','error','unknown','waiting_for_browser'}
POOL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS wf_apply_slots (
 worker_id TEXT PRIMARY KEY,
 task_id TEXT UNIQUE REFERENCES wf_tasks(id),
 state TEXT NOT NULL DEFAULT 'available', stage TEXT NOT NULL DEFAULT 'idle',
 stage_started_at TEXT, updated_at TEXT,
 answer_hash TEXT, answer_revision INTEGER,
 legacy_affinity TEXT
);
CREATE TABLE IF NOT EXISTS wf_apply_pool_meta (
 id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wf_apply_tab_bindings (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES wf_tasks(id),
 process_id TEXT NOT NULL REFERENCES wf_processes(id), worker_id TEXT NOT NULL,
 native_thread_id TEXT NOT NULL, tab_id TEXT NOT NULL, browser_id TEXT NOT NULL,
 job_key TEXT NOT NULL, job_url TEXT NOT NULL, material_hash TEXT NOT NULL,
 receipt_path TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, retired_at TEXT, retirement_evidence TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS wf_apply_tab_task ON wf_apply_tab_bindings(task_id) WHERE retired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS wf_apply_tab_owner ON wf_apply_tab_bindings(browser_id,tab_id) WHERE retired_at IS NULL;
INSERT OR IGNORE INTO wf_apply_pool_meta(id,revision) VALUES(1,0);
'''


def browser_resource(resource):
    return resource == 'browser' or (isinstance(resource, str) and resource.startswith('browser:apply-'))


def slot_for_task(task):
    checkpoint = task.get('checkpoint', {})
    if isinstance(checkpoint, str):
        checkpoint = json.loads(checkpoint)
    value = checkpoint.get('application_worker_id')
    return value if value in WORKER_IDS else None


def current_transport_proof(store,db=None):
    """Only the host's current transport identity can authorize native dispatch."""
    provider=getattr(store,'application_pool_transport_identity',None)
    if not callable(provider):
        return None
    from application_pool_proof import resolve
    try:
        identity=provider()
        return resolve(db if db is not None else store,identity)
    except (ReviewError,OSError,ValueError):
        return None


class ApplicationPoolMixin:
    def _application_tab_context(self, db, task_id, token, worker_id):
        task=self._leased_task(db,task_id,token)
        slot=db.execute('SELECT task_id FROM wf_apply_slots WHERE worker_id=?',(worker_id,)).fetchone()
        if (worker_id not in WORKER_IDS or slot is None or slot['task_id']!=task_id
                or slot_for_task(dict(task))!=worker_id or task['resource']!='browser:'+worker_id):
            raise ReviewError('Application tab owner does not match its leased worker.',409)
        process,unused=self._application_lease(db,json.loads(task['payload'])['process_id'],token)
        self._check_application(db,process)
        return process,json.loads(process['snapshot'])

    def application_tab_binding(self, task_id, token, worker_id, *, native_thread_id=None,
                                requested_tab_id=None, expected_binding_id=None, required=False):
        """Host admission, before native dispatch. This does not mediate arbitrary CUA calls."""
        with self.transaction() as db:
            process,snapshot=self._application_tab_context(db,task_id,token,worker_id)
            binding=db.execute('SELECT * FROM wf_apply_tab_bindings WHERE task_id=? AND retired_at IS NULL',
                               (task_id,)).fetchone()
            if binding is None:
                if required or requested_tab_id is not None:
                    raise ReviewError('Application has no host-bound tab. Open its own tab first.',409)
                return None
            if (binding['process_id']!=process['id'] or binding['worker_id']!=worker_id
                    or binding['job_key']!=process['job_key']
                    or binding['job_url']!=canonical_url(snapshot['url'])
                    or binding['material_hash']!=snapshot['material_hash']
                    or expected_binding_id is not None and binding['id']!=expected_binding_id
                    or native_thread_id is not None and binding['native_thread_id']!=native_thread_id
                    or requested_tab_id is not None and binding['tab_id']!=requested_tab_id):
                raise ReviewError('Application tab binding changed or belongs to another worker.',409)
            return dict(binding)

    def bind_application_tab(self, task_id, token, worker_id, result, *, receipt_path):
        """Host-only opening-turn commit. No checkpoint/HTTP answer writes this table."""
        from codex_transport import _private_json, _safe_uuid
        receipt_path=Path(receipt_path)
        receipt=_private_json(receipt_path)
        saved_result=_private_json(receipt_path.parent/'result.json')
        if (saved_result!=result or receipt.get('role')!='application_tab_open'
                or receipt.get('status')!='completed' or receipt.get('transport')!='app-server'
                or receipt.get('task_id')!=task_id or receipt.get('application_worker_id')!=worker_id
                or receipt.get('lease_digest')!=hashlib.sha256(token.encode()).hexdigest()
                or receipt.get('browser') is not True or receipt.get('session_worker') is not False
                or type(receipt.get('exit_code')) is not int or receipt['exit_code']!=0
                or receipt.get('turn_completed') is not True or receipt.get('browser_verified') is not True
                or not _safe_uuid(receipt.get('thread_id')) or not _safe_uuid(receipt.get('turn_id'))
                or result.get('outcome')!='opened' or result.get('created_new') is not True
                or result.get('handoff_preserved') is not True
                or result.get('blocker') or result.get('blocker_reason')):
            raise ReviewError('Application tab opening has no matching native receipt.',409)
        tab_id=text(result.get('tab_id'),'Application tab ID',500,required=True)
        with self.transaction(True) as db:
            process,snapshot=self._application_tab_context(db,task_id,token,worker_id)
            if canonical_url(result.get('job_url'))!=canonical_url(snapshot['url']):
                raise ReviewError('Application tab opening job does not match.',409)
            if db.execute('SELECT 1 FROM wf_apply_tab_bindings WHERE retired_at IS NULL AND (task_id=? OR tab_id=?)',
                          (task_id,tab_id)).fetchone():
                raise ReviewError('Application tab is already bound. It cannot be adopted or replaced.',409)
            binding={'id':str(uuid.uuid4()),'task_id':task_id,'process_id':process['id'],'worker_id':worker_id,
                'native_thread_id':receipt['thread_id'],'tab_id':tab_id,'browser_id':'chrome:current-profile',
                'job_key':process['job_key'],'job_url':canonical_url(snapshot['url']),
                'material_hash':snapshot['material_hash'],'receipt_path':str(receipt_path.resolve()),
                'receipt_sha256':hashlib.sha256(receipt_path.read_bytes()).hexdigest(),'created_at':now()}
            db.execute('INSERT INTO wf_apply_tab_bindings('+','.join(binding)+') VALUES('+','.join('?' for _ in binding)+')',
                       tuple(binding.values()))
            self._process_event(db,process['id'],'application_tab_bound',binding,'host')
            return binding

    def initialize_application_pool(self):
        with self.connect() as db:
            db.executescript(POOL_SCHEMA)
            for worker_id in WORKER_IDS:
                db.execute('INSERT OR IGNORE INTO wf_apply_slots(worker_id,updated_at) VALUES(?,?)',
                           (worker_id, now()))

    @staticmethod
    def _pool_bump(db):
        db.execute('UPDATE wf_apply_pool_meta SET revision=revision+1 WHERE id=1')

    def _prepare_slot_candidate(self, db, worker_id, rows):
        """Select/retain one task while the queue write transaction is held."""
        if worker_id not in WORKER_IDS:
            raise ReviewError('Unknown application worker.')
        proof=current_transport_proof(self,db)
        if getattr(self,'application_pool_proof_required',False) and proof is None:
            return None
        slot = db.execute('SELECT * FROM wf_apply_slots WHERE worker_id=?',(worker_id,)).fetchone()
        if slot is None:
            raise ReviewError('Application pool is not initialized.',409)
        if slot['task_id']:
            current = self._task(db, slot['task_id'])
            process = db.execute('SELECT submission_status FROM wf_processes WHERE task_id=?',
                                 (slot['task_id'],)).fetchone()
            terminal = (current['status'] in ('succeeded','cancelled') or
                        process and process['submission_status']=='submitted')
            if terminal and not db.execute('SELECT 1 FROM wf_resource_locks WHERE task_id=?',
                                            (slot['task_id'],)).fetchone():
                db.execute("UPDATE wf_apply_slots SET task_id=NULL,state='available',stage='idle',"
                           'answer_hash=NULL,answer_revision=NULL,updated_at=? WHERE worker_id=?',
                           (now(), worker_id))
                self._pool_bump(db)
            else:
                candidate=next((row for row in rows if row['id']==slot['task_id']), None)
                if proof is not None and candidate is not None:
                    checkpoint=json.loads(current['checkpoint'])
                    checkpoint['pool_transport_proof']=proof
                    db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?',(encode(checkpoint),current['id']))
                    return self._task(db,current['id'])
                return candidate
        # A legacy global owner can still touch any profile tab. Drain it before
        # assigning isolated work; do not hide old quarantine by renaming it.
        if (not getattr(self,'browser_access_serialized',False) and
                db.execute("SELECT 1 FROM wf_resource_locks WHERE resource='browser'").fetchone()):
            return None
        assigned = {r[0] for r in db.execute('SELECT task_id FROM wf_apply_slots WHERE task_id IS NOT NULL')}
        for row in rows:
            if row['kind'] != 'apply' or row['id'] in assigned:
                continue
            cp = json.loads(row['checkpoint'])
            if cp.get('submission_started'):
                continue
            reset=cp.get('pool_session_reset')
            if isinstance(reset,dict) and reset.get('worker_id') != worker_id:
                continue
            if db.execute('SELECT 1 FROM wf_resource_locks WHERE task_id=?',(row['id'],)).fetchone():
                continue
            cp['application_worker_id'] = worker_id
            if proof is not None:
                cp['pool_transport_proof']=proof
            resource = 'browser:' + worker_id
            db.execute('UPDATE wf_tasks SET resource=?,checkpoint=? WHERE id=?',
                       (resource, encode(cp), row['id']))
            stamp = now()
            db.execute("UPDATE wf_apply_slots SET task_id=?,state='ready',stage='ready',"
                       'stage_started_at=?,updated_at=? WHERE worker_id=?',
                       (row['id'], stamp, stamp, worker_id))
            self._pool_bump(db)
            return self._task(db, row['id'])
        return None

    def application_stage(self, worker_id, stage, *, state=None, task_id=None, lease_token=None):
        if worker_id not in WORKER_IDS or stage not in STAGES or state is not None and state not in STATES:
            raise ReviewError('Invalid application workflow stage.')
        with self.transaction(True) as db:
            if lease_token is not None:
                self._leased_task(db,task_id,lease_token)
            slot = db.execute('SELECT * FROM wf_apply_slots WHERE worker_id=?',(worker_id,)).fetchone()
            if slot is None or task_id is not None and slot['task_id'] != task_id:
                raise ReviewError('Application slot ownership changed.',409)
            new_state = state or slot['state']
            if slot['stage']==stage and slot['state']==new_state:
                return
            stamp = now()
            db.execute('UPDATE wf_apply_slots SET stage=?,state=?,stage_started_at=?,updated_at=? WHERE worker_id=?',
                       (stage,new_state,stamp,stamp,worker_id))
            self._pool_bump(db)

    def refresh_application_slot(self, worker_id):
        with self.transaction(True) as db:
            slot = db.execute('SELECT * FROM wf_apply_slots WHERE worker_id=?',(worker_id,)).fetchone()
            if slot is None or not slot['task_id']:
                return
            task = self._task(db,slot['task_id'])
            process = db.execute('SELECT submission_status FROM wf_processes WHERE task_id=?',
                                 (task['id'],)).fetchone()
            if task['resource_quarantined'] or process and process['submission_status']=='submission_unknown':
                state,stage='paused','reconciling'
            elif task['status'] in ('queued','retry_wait'):
                state,stage='ready','ready'
            elif task['status'] in ('waiting_for_user','failed'):
                state,stage='paused','paused'
            elif task['status'] in ('succeeded','cancelled'):
                state,stage='available','completed'
            else:
                return
            if (slot['state'],slot['stage'])!=(state,stage):
                stamp=now()
                db.execute('UPDATE wf_apply_slots SET state=?,stage=?,stage_started_at=?,updated_at=? WHERE worker_id=?',
                           (state,stage,stamp,stamp,worker_id))
                self._pool_bump(db)

    def application_workflow(self):
        """One consistent read of durable metadata, plus bounded lock observation."""
        pool = getattr(self,'question_pool',None)
        with self.transaction() as db:
            meta=db.execute('SELECT revision FROM wf_apply_pool_meta WHERE id=1').fetchone()
            workers=[]
            for row in db.execute('SELECT * FROM wf_apply_slots ORDER BY worker_id'):
                task=db.execute('SELECT * FROM wf_tasks WHERE id=?',(row['task_id'],)).fetchone() if row['task_id'] else None
                process=db.execute('SELECT * FROM wf_processes WHERE task_id=?',(row['task_id'],)).fetchone() if task else None
                job=json.loads(process['snapshot']) if process else {}
                questions=[r[0] for r in db.execute('SELECT id FROM wf_questions WHERE process_id=? AND superseded=0 AND answer IS NULL',
                                                    (process['id'],))] if process else []
                workers.append({'worker_id':row['worker_id'],'state':row['state'],'stage':row['stage'],
                    'process_id':process['id'] if process else None,'task_id':row['task_id'],
                    'company':job.get('company_name') or job.get('company_id'), 'job_title':job.get('title'),
                    'stage_started_at':row['stage_started_at'],'updated_at':row['updated_at'], 'question_ids':questions})
            pending=db.execute('SELECT COUNT(*) FROM wf_questions WHERE superseded=0 AND answer IS NULL').fetchone()[0]
            answered=db.execute('SELECT COUNT(*) FROM wf_questions WHERE answer IS NOT NULL').fetchone()[0]
        result = {'revision':meta['revision'] if meta else 0,'generated_at':now(),'pool_size':POOL_SIZE,'workers':workers,
            'question_pool':{'pending_count':pending,'answered_count':answered,'revision':meta['revision'] if meta else 0},
            'question_lock':{'state':'free','owner_kind':None,'owner_id':None,'acquired_at':None,'waiting_worker_ids':[]},
            'answer_file':{'revision':None,'hash_algorithm':None,'hash':None},
            'answer_handler':getattr(self,'_answer_handler_state',{'stage':'idle','updated_at':None})}
        if pool is not None:
            details=pool.snapshot()
            result.update(details)
            result['revision'] += details['question_pool']['revision']
            with self.transaction() as db:
                lock=db.execute('SELECT generation,token FROM wf_qpool_lock WHERE singleton=1').fetchone()
            result['revision'] += 2*lock['generation'] + int(lock['token'] is None)
        observer = getattr(self, 'browser_access_snapshot', None)
        if callable(observer):
            access = observer()
            with self.transaction() as db:
                blockers = [{'task_id': row['task_id'], 'resource': row['resource']}
                    for row in db.execute('SELECT task_id,resource FROM wf_resource_locks '
                                          'WHERE quarantined=1 ORDER BY resource')
                    if browser_resource(row['resource'])]
            access['blocked_by'] = blockers
            if blockers and access['owner'] is None:
                access['state'] = 'blocked'
            result['browser_access'] = access
            result['revision'] += access['revision']
        return result
