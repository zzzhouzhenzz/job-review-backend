"""Review parked questions without Chrome; apply a private, source-checked plan.

This operator command does not initialize a database or start workers. The running
backend must have completed its normal backed-up schema migration before apply.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3

from codex_runtime import load_runtime
from store import ReviewError, digest, encode
from workflow import WorkflowStore


class QuestionStore(WorkflowStore):
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db


def pending_context(store, process_id):
    with store.transaction() as db:
        process = store._process(db, process_id)
        store._check_application(db, process)
        task = store._task(db, process['task_id'])
        browser_blocked = store._waiting_browser_question_blocker(db, process, task)
        if (process['submission_status'] != 'preparing'
                or process['block_reason'] != 'questions_pending' and not browser_blocked
                or task['status'] != 'waiting_for_user' or task['resource_quarantined']):
            raise ReviewError('Application is not waiting for an evidence review.', 409)
        questions = [store.question(row) for row in db.execute(
            'SELECT * FROM wf_questions WHERE process_id=? AND superseded=0 AND answer IS NULL ORDER BY id',
            (process_id,))]
        return {'process': store.process(process), 'questions': questions,
                'question_bindings': json.loads(task['checkpoint']).get('question_bindings', {})}


class ReviewLease:
    """Read-only revision checks while a browser-free review is running."""
    def __init__(self, store, context, evidence):
        self.store, self.context, self.evidence = store, context, evidence
        self.task_id = context['process']['task_id']

    def heartbeat(self):
        current = pending_context(self.store, self.context['process']['id'])
        if digest(current) != digest(self.context):
            raise ReviewError('Pending questions changed during evidence review.', 409)
        self.evidence.assert_current()
        return True


def review_pending(store, runtime):
    with store.transaction() as db:
        ids = [row[0] for row in db.execute(
            """SELECT p.id FROM wf_processes p JOIN wf_tasks t ON t.id=p.task_id
            WHERE p.submission_status='preparing' AND t.status='waiting_for_user'
            AND EXISTS(SELECT 1 FROM wf_questions q WHERE q.process_id=p.id AND q.superseded=0
                       AND q.answer IS NULL) ORDER BY p.id""")]
    plans = []
    for process_id in ids:
        try:
            context = pending_context(store, process_id)
        except ReviewError as error:
            if error.status == 409:
                continue
            raise
        evidence = runtime.evidence_snapshot(application=True)
        lease = ReviewLease(store, context, evidence)
        answers, provenance, unresolved = runtime.review_questions(
            context, context['questions'], context['question_bindings'], evidence, lease)
        plans.append({'context': context, 'source_snapshot_hash': evidence.hash,
                      'answers': answers, 'provenance': provenance, 'unresolved': unresolved})
    return {'schema_version': 1, 'database': str(store.path), 'plans': plans}


def apply_plan(store, runtime, plan):
    store.application_profile = runtime.application_profile
    if plan.get('schema_version') != 1 or plan.get('database') != str(store.path):
        raise ReviewError('Evidence plan does not match this database.')
    receipts = []
    for item in plan['plans']:
        if not item['answers']:
            continue
        context = item['context']
        evidence = runtime.evidence_snapshot(application=True)
        if evidence.hash != item['source_snapshot_hash']:
            raise ReviewError('Applicant evidence changed after the review.', 409)
        # Recheck the complete reviewed proposal, not just its cited source hashes.
        resolutions = []
        for answer in item['answers']:
            prov = item['provenance'][answer['question_id']]
            if prov['source_snapshot_hash'] != evidence.hash:
                raise ReviewError('Answer provenance does not match the review.')
            resolutions.append({**prov, 'question_id': answer['question_id'],
                'question_revision': answer['question_revision'], 'answer_json': encode(answer['value'])})
        questions = {q['id']: q for q in context['questions']}
        resolutions += [{'question_id': q['question_id'], 'question_revision': questions[q['question_id']]['revision'],
                         'method': 'unresolved', 'answer_json': 'null', 'citations': [], 'explanation': q['reason']}
                        for q in item['unresolved']]
        answers, provenance, _ = evidence.validate_review({'resolutions': resolutions},
            context['questions'], context['question_bindings'])
        # The writer enforces live process/form/question revisions and bindings
        # under BEGIN IMMEDIATE. Exact retries use its durable receipt.
        receipts.append(runtime.save_evidence_answers(store, context['process'], answers, provenance, evidence))
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['review', 'apply'])
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--runtime-config', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path, help='Private review plan; review never overwrites it.')
    args = parser.parse_args()
    store, runtime = QuestionStore(args.db), load_runtime(args.runtime_config)
    try:
        if args.action == 'review':
            if args.plan.exists():
                raise ReviewError('The review plan already exists.')
            plan = review_pending(store, runtime)
            fd = os.open(args.plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(encode(plan) + '\n')
            print(encode({'reviewed_processes': len(plan['plans']),
                          'supported_answers': sum(len(p['answers']) for p in plan['plans']),
                          'unresolved': sum(len(p['unresolved']) for p in plan['plans'])}))
        else:
            receipts = apply_plan(store, runtime, json.loads(args.plan.read_text()))
            print(encode({'accepted_processes': len(receipts)}))
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
