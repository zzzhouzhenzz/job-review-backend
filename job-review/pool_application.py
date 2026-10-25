"""Host integration for worker-owned question matching and answer publication."""
from __future__ import annotations
import json
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from store import ReviewError, encode, now
from application_pool import slot_for_task

MATCH_SCHEMA={'type':'object','properties':{'matches':{'type':'array','maxItems':50,'items':{
    'type':'object','properties':{'question_id':{'type':'string'},'pool_question_id':{'type':'string'}},
    'required':['question_id','pool_question_id'],'additionalProperties':False}}},'required':['matches'],'additionalProperties':False}
FORMAT_SCHEMA={'type':'object','properties':{'append_text':{'type':'string','maxLength':200000}},
               'required':['append_text'],'additionalProperties':False}


class FormattingLease:
    """Local model-only invocation; cannot acquire a browser or submit a task."""
    def __init__(self, store, payload, transport):
        self.store=store
        self.task_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'question-format:'+payload['request_id']))
        self.token=str(uuid.uuid4())
        self.transport=transport
        self.application_worker_id=None
    def heartbeat(self):
        if getattr(self.transport,'_closed',threading.Event()).is_set():
            raise ReviewError('Answer formatter stopped before saving.',503)
        return True


def handler_stage(store, stage):
    previous=getattr(store,'_answer_handler_state',{}).get('stage')
    store._answer_handler_state={'stage':stage,'updated_at':now()}
    if previous != stage:
        with store.transaction(True) as db:
            store._pool_bump(db)


def initialize(runtime, store):
    from question_pool import SharedQuestionPool
    from application_pool_proof import collect_identity
    store.application_pool_transport_identity=lambda: collect_identity(
        runtime.transport,runtime.pool_configuration_identity(),
        codex_bundle=Path('/Applications/ChatGPT.app'))
    store.application_pool_proof_required=True
    store.question_pool=SharedQuestionPool(store).initialize()
    store.answer_formatter=lambda specification,payload: format_answers(runtime,store,specification,payload)
    handler_stage(store,'idle')
    with store.question_pool.hold('recovery','startup'):
        recover_locked(store)
        store.question_pool.answer_snapshot()


def recover_locked(store):
    """Finish durable fan-out after a crash before a shared reader proceeds."""
    pool=store.question_pool
    affected=apply_linked_answers(store,pool.linked_answer_requests())
    pool.repair()
    for worker in ('apply-1','apply-2','apply-3'):
        store.refresh_application_slot(worker)
    return affected


def recover(store):
    pool=getattr(store,'question_pool',None)
    if pool is None:
        return []
    try:
        with pool.hold('recovery','scheduled-recovery',timeout_seconds=.01):
            return recover_locked(store)
    except ReviewError as error:
        if error.reason == 'question_pool_busy':
            return []
        raise


def format_answers(runtime,store,specification,payload):
    handler_stage(store,'formatting')
    lease=FormattingLease(store,payload,runtime.transport)
    prompt=('Format only the exact confirmed answer batch below as Markdown. Do not browse, use tools, '
        'edit files, infer facts, broaden scope, or change signed metadata. Return append_text containing '
        'the complete specified Markdown. Preserve the original question, answer, scope and markers exactly. '
        'This is a short formatting operation, not question matching.\n'+encode(specification))
    result=runtime.transport.run('answer_markdown_format',prompt,FORMAT_SCHEMA,lease,browser=False)
    return result['append_text']


def apply_linked_answers(store, requests):
    affected=[]
    for request in requests:
        payload=request['payload']
        try:
            receipt=store._answer_questions(payload,mutation_fence=store.question_pool._assert,
                                           shared_sources=request['sources'])
        except ReviewError as error:
            # Only a concurrent legitimate revision change may invalidate one
            # candidate. Do not bypass the existing authorization/submission gates.
            if error.status != 409:
                raise
            with store.transaction(True) as db:
                store.question_pool._assert(db)
                store._process_event(db,payload['process_id'],'shared_answer_deferred',
                    {'request_id':payload['request_id'],'reason':str(error)},'question_pool')
            continue
        with store.transaction(True) as db:
            store.question_pool._assert(db)
            store._process_event(db,payload['process_id'],'shared_answer_propagated',
                {'request_id':payload['request_id'],'sources':request['sources']},'question_pool')
        affected.extend(receipt['process_ids'])
    return sorted(set(affected))


def answer_questions(store,payload):
    pool=store.question_pool
    handler_stage(store,'formatting')
    try:
        spec=pool.formatting_spec(payload)
        # Equivalent stale-device answers can be acknowledged without a model
        # call. Core validation still rejects a conflicting stale value.
        if spec.get('already_answered') or spec.get('receipt'):
            with pool.hold('answer_handler',payload['request_id']):
                spec=pool.formatting_spec(payload)
                if not (spec.get('already_answered') or spec.get('receipt')):
                    raise ReviewError('The saved answer changed. Refresh the questions.',409)
                handler_stage(store,'checking_blockers')
                affected=apply_linked_answers(store,pool.linked_answer_requests())
                pool.repair()
                for worker in ('apply-1','apply-2','apply-3'):
                    store.refresh_application_slot(worker)
                receipt=spec.get('receipt') or {'request_id':payload['request_id'],'accepted':True,
                    'process_ids':[payload['process_id']], 'task_ids':[]}
                receipt={**receipt,'already_answered':bool(spec.get('already_answered')),
                    'affected_process_ids':sorted(set(affected+[payload['process_id']]))}
            store.sync_profile_answers()
            return receipt
        append_text=store.answer_formatter(spec,payload)
        handler_stage(store,'waiting_for_lock')
        with pool.hold('answer_handler',payload['request_id']):
            handler_stage(store,'saving_answers')
            saved=pool.save_answered_questions(append_text)
            handler_stage(store,'checking_blockers')
            affected=apply_linked_answers(store,saved['answer_requests'])
            handler_stage(store,'publishing_answers')
            pool.repair()
            with store.transaction() as db:
                process=store._process(db,payload['process_id'])
                task_ids=[process['task_id']]
            for worker in ('apply-1','apply-2','apply-3'):
                store.refresh_application_slot(worker)
            receipt={'request_id':payload['request_id'],'accepted':True,'process_ids':[payload['process_id']],
                'task_ids':task_ids,'affected_process_ids':sorted(set(affected+[payload['process_id']])),
                'already_answered':saved.get('already_answered',False)}
        store.sync_profile_answers()
        return receipt
    except Exception:
        handler_stage(store,'error')
        raise
    finally:
        if getattr(store,'_answer_handler_state',{}).get('stage') != 'error':
            handler_stage(store,'idle')


def load_answers(store,task,lease):
    pool=getattr(store,'question_pool',None)
    if pool is None or not lease.application_worker_id:
        return None
    lease.stage('waiting_for_lock')
    with pool.hold('worker',lease.application_worker_id):
        lease.stage('loading_answers')
        snapshot=pool.answer_snapshot(task['checkpoint'].get('answer_file_hash'))
        manifest=snapshot['manifest']
        lease.checkpoint({'answer_file_hash':manifest['digest'],'answer_file_revision':manifest['revision']})
        task['checkpoint'].update(answer_file_hash=manifest['digest'],answer_file_revision=manifest['revision'])
        return snapshot


def register_questions(runtime,store,task,lease,context,questions,bindings,form_version,saved, *, browser_blocker=None):
    """Match under the shared lock in the original applying native session."""
    pool=store.question_pool
    lease.stage('waiting_for_lock')
    with pool.hold('worker',lease.application_worker_id) as token:
        lease.pool_guard=lambda: pool.renew(token,lease_seconds=120)
        try:
            lease.stage('checking_hash')
            snapshot=pool.answer_snapshot(task['checkpoint'].get('answer_file_hash'))
            if snapshot['changed']:
                lease.stage('refreshing_answers')
            # The same worker sees pending and answered canonical questions, so
            # an answer that arrived while it waited cannot be recreated.
            pending=pool.pending_questions()
            full=pool.answer_snapshot()
            latest={record['pool_id']:record for record in full.get('records',[])}
            answered=[{**record,'answer_record_id':record['id'],'id':pool_id}
                      for pool_id,record in latest.items()]
            lease.stage('matching_questions')
            result=runtime.transport.run('application_pool_match',
                'You are the same applying worker for this application. Match each new question only '
                'to an equivalent canonical pending or answered question in the supplied pool. Compare '
                'meaning, full choices, and scope. Keep employer legal consent and application-specific '
                'disclosures scoped. Return only matches with supplied IDs; omit unmatched questions. '
                'Do not invent an answer, use tools, browse, edit a form or file, or submit anything.\n'+
                encode({'process':context['process'],'questions':questions,'pending_questions':pending,
                    'answered_questions':answered,'new_answer_snapshot':snapshot['changed']}),
                MATCH_SCHEMA,lease,browser=False,session_worker=True)
            valid={q['id'] for q in questions};matches={}
            for match in result['matches']:
                qid=match['question_id']
                if qid not in valid or qid in matches:
                    raise ReviewError('Worker returned invalid question matches.',409)
                matches[qid]=match['pool_question_id']
            lease.heartbeat()
            lease.stage('saving_questions')
            lease.checkpoint({'question_bindings':bindings,'answer_file_hash':full['manifest']['digest'],
                              'answer_file_revision':full['manifest']['revision']})
            process=store.ask_questions(context['process']['id'],task['lease_token'],form_version,
                                        questions,preserve_answer_ids=list(saved),browser_blocker=browser_blocker)
            registered=pool.register_questions(process['id'],lease.application_worker_id,questions,matches=matches)
            apply_linked_answers(store,registered['answer_requests'])
            pool.repair()
            store.refresh_application_slot(lease.application_worker_id)
        finally:
            del lease.pool_guard
    store.sync_profile_answers()
