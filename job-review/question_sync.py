"""One canonical question snapshot for all clients. No model or browser calls.

SQLite is authoritative. Markdown and per-form answer columns are projections.
The revision signal is durable and monotonic; polling/reconnect replaces a push
bus. A busy snapshot never supplies replacement arrays to a client.
"""
from __future__ import annotations

import json

from answer_reuse import normalize
from store import ReviewError, now


def mapped_answer(record, accepted):
    """Map canonical accepted labels to this record's option IDs, or fail closed."""
    if accepted is None:
        return None
    labels = json.loads(accepted['labels'])
    if record['type'] == 'text':
        if len(labels) == 1 and isinstance(labels[0], str):
            return labels[0]
    else:
        choices = record['choices']
        if isinstance(choices, str):
            choices = json.loads(choices)
        options = {normalize(choice['label']): choice['id'] for choice in choices}
        if len(options) == len(choices) and all(normalize(label) in options for label in labels):
            values = [options[normalize(label)] for label in labels]
            if record['type'] == 'single_choice' and len(values) == 1:
                return values[0]
            if record['type'] == 'multi_choice' and len(values) == len(set(values)):
                return values
    raise ReviewError('The canonical answer cannot map to this form. Refresh before answering.',
                      409, reason='question_sync_stale')


def canonical_binding(db, question_id):
    link = db.execute('SELECT * FROM wf_qpool_links WHERE question_id=?', (question_id,)).fetchone()
    if link is None:
        return None, None
    accepted = db.execute('SELECT * FROM wf_qpool_answers WHERE pool_id=? ORDER BY id DESC LIMIT 1',
                          (link['pool_id'],)).fetchone()
    return link, accepted


def overlay_questions(db, questions, revision):
    """Legacy form views reference canonical values; never arbitrate form copies."""
    result = []
    for question in questions:
        link, accepted = canonical_binding(db, question['id'])
        item = {**question, 'pool_question_id': link['pool_id'] if link else None,
                'pool_revision': revision, 'pool_answer_revision': accepted['id'] if accepted else 0}
        if link is not None:
            item['answer'] = mapped_answer(question, accepted)
            item['answered_at'] = accepted['created_at'] if accepted else None
        result.append(item)
    return result


def snapshot(store):
    """Return a coherent read immediately, including while a writer holds the lock.

    Do not take the writer lock to poll. Existing worker/legacy-read recovery
    repairs interrupted writes. Pending state explicitly retains the old client
    snapshot until that repair or registration has completed.
    """
    if getattr(store, 'question_pool', None) is None:
        raise ReviewError('Shared question synchronization is not configured.', 404)
    with store.transaction() as db:
        lock = db.execute('SELECT * FROM wf_qpool_lock WHERE singleton=1').fetchone()
        state = db.execute('SELECT * FROM wf_qpool_state WHERE singleton=1').fetchone()
        # Acquisition increments generation; release adds one. Business commits
        # only increase revision. A reader never increments either counter.
        signal = 2 * lock['generation'] + state['revision'] + int(lock['token'] is None)
        status = ('updating' if lock['token'] is not None and lock['expires_at'] > now()
                  else 'pending' if lock['token'] is not None or state['published_revision'] != state['revision']
                  else 'ready')
        result = {'sync': {'state': status, 'revision': signal, 'pool_revision': state['revision']},
                  'shared_questions': None, 'questions': None, 'processes': None,
                  'historical_unlinked_question_ids': None}
        if status != 'ready':
            return result
        forms = [store.question(row) for row in db.execute(
            'SELECT * FROM wf_questions WHERE superseded=0 ORDER BY created_at,id')]
        questions = overlay_questions(db, forms, state['revision'])
        historical, linked = [], []
        for question in questions:
            if question['pool_question_id'] is not None:
                linked.append(question)
                continue
            process = store._process(db, question['process_id'])
            if ((question['answer'] is not None and question['answered_at'])
                    or process['submission_status'] == 'submitted'):
                # Keep these legacy records in their original audit tables/API.
                # They cannot become a second canonical answer source here.
                historical.append(question['id'])
            else:
                result['sync']['state'] = 'pending'
                return result
        questions = linked
        bindings = {}
        for question in questions:
            bindings.setdefault(question['pool_question_id'], []).append(question['id'])
        shared = []
        for row in db.execute('SELECT * FROM wf_qpool_questions ORDER BY created_at,id'):
            pool_id = row['id']
            question_ids = bindings.get(pool_id, [])
            record = json.loads(row['record'])
            accepted = db.execute('SELECT * FROM wf_qpool_answers WHERE pool_id=? ORDER BY id DESC LIMIT 1',
                                  (pool_id,)).fetchone()
            shared.append({'id': pool_id, **record,
                'scope': {'kind': row['scope_kind'], 'key': row['scope_key']},
                'status': 'answered' if accepted else 'open', 'answer': mapped_answer(record, accepted),
                'answer_revision': accepted['id'] if accepted else 0,
                'answered_at': accepted['created_at'] if accepted else None,
                'binding_ids': question_ids})
        result.update(shared_questions=shared, questions=questions, historical_unlinked_question_ids=historical,
            processes=[store._process_view(db, row) for row in db.execute(
                'SELECT * FROM wf_processes ORDER BY created_at DESC,id').fetchall()])
        return result
