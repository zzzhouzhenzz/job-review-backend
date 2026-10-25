"""Operator-only recovery for a proven unstarted submission turn.

No scheduler or HTTP route invokes this module. A missing receipt is never proof.
The input is a private, previously recorded stopped-worker audit, bound to the
exact process, task, attempt and native turn. Historical attempts stay intact.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

from codex_transport import _private_json
from shared_browser_session import verified_record
from store import ReviewError, digest, encode, now, text


def _require(condition, message):
    if not condition:
        raise ReviewError(message, 409)


def requeue_unstarted_submission(store, *, process_id, process_revision,
        task_updated_at, attempt_id, audit_path, run_directory, authorization):
    """Requeue only after explicit operator authorization and positive no-action proof.

The caller backs up the database first. The old process and attempt are retained;
ordinary preparation, current answers, approvals and final-submit checks run again.
This method cannot recover a turn containing even one external tool action.
"""
    authorization = text(authorization, 'Recovery authorization', 2000, required=True)
    audit_path = Path(audit_path).resolve(strict=True)
    runs = Path(run_directory).resolve(strict=True)
    audit = _private_json(audit_path)
    proof = audit.get('stopped_turn_proof', {})
    _require(audit.get('phase') == 'quarantine_cleared'
        and audit.get('process_id') == process_id and audit.get('attempt_id') == attempt_id
        and audit.get('submission_replayed') is False,
        'Recovery audit does not identify this stopped submission attempt.')
    _require(proof.get('version') == 1 and proof.get('stable_snapshots') == 2
        and proof.get('inspector_exit_code') == 0 and proof.get('items_view') == 'full'
        and proof.get('turn_status') in {'interrupted', 'failed', 'completed'}
        and proof.get('item_count') == 1 and proof.get('item_type_counts') == {'userMessage': 1}
        and proof.get('external_item_count') == 0 and proof.get('child_count') == 0
        and type(proof.get('started_at')) is int and type(proof.get('completed_at')) is int
        and 0 < proof['started_at'] <= proof['completed_at'],
        'Recovery requires two full terminal snapshots with only the user message.')
    try:
        observed = datetime.fromisoformat(proof.get('observed_at', '').replace('Z', '+00:00'))
        _require(observed.tzinfo is not None and proof['completed_at'] <= observed.timestamp()
            <= datetime.now(timezone.utc).timestamp(), 'The stopped-turn observation time is invalid.')
    except (TypeError, ValueError):
        raise ReviewError('The stopped-turn observation time is missing or invalid.', 409) from None
    previous = audit.get('restored_record', {})
    prior_receipt = verified_record(runs, previous)
    _require(previous.get('task_id') == audit.get('task_id')
        and previous.get('thread_id') == proof.get('thread_id')
        and prior_receipt and prior_receipt.get('role') == 'application_prepare',
        'The stopped worker does not match the preserved preparation session.')
    # Find only receipts for this exact task and turn. A missing, duplicate, or
    # completed submit result is not eligible for this narrow recovery.
    matching = []
    for path in runs.glob('application_submit-*/receipt.json'):
        receipt = _private_json(path)
        failure = receipt.get('failure') or {}
        if (receipt.get('task_id') == audit['task_id']
                and failure.get('turn_id') == proof.get('turn_id')):
            matching.append(receipt)
    _require(len(matching) == 1, 'The exact failed submit receipt is missing or ambiguous.')
    receipt = matching[0]
    _require(receipt.get('status') == 'failed' and receipt.get('role') == 'application_submit'
        and receipt.get('transport') == 'app-server' and receipt.get('browser') is True
        and receipt.get('thread_id') == proof['thread_id']
        and receipt.get('lease_digest') == previous.get('lease_digest')
        and receipt.get('turn_completed') is False and receipt.get('tools') == []
        and receipt.get('browser_verified') is False
        and receipt['failure'].get('reason') == 'lease_lost',
        'The submit receipt does not support an unstarted turn.')
    with store.transaction(True) as db:
        process = store._process(db, process_id)
        store._revision(process, process_revision)
        task = store._task(db, process['task_id'])
        checkpoint = json.loads(task['checkpoint'])
        _require(process['task_id'] == audit['task_id'] and task['kind'] == 'apply'
            and json.loads(task['payload']).get('process_id') == process_id
            and process['submission_status'] == process['block_reason'] == 'submission_unknown'
            and process['receipt'] is None and task['error'] == 'submission_unknown'
            and task['status'] == 'waiting_for_user' and task['updated_at'] == task_updated_at
            and task['lease_token'] is None and task['lease_until'] is None
            and not task['resource_quarantined']
            and task['failure_count'] < task['max_attempts'],
            'The application changed or is not eligible for stopped-attempt recovery.')
        _require(not db.execute("SELECT 1 FROM wf_resource_locks WHERE task_id=? OR quarantined=1",
                    (task['id'],)).fetchone(), 'A browser quarantine or task lock still exists.')
        attempts = db.execute('SELECT * FROM wf_submission_attempts WHERE process_id=?', (process_id,)).fetchall()
        _require(len(attempts) == 1 and attempts[0]['id'] == attempt_id
            and attempts[0]['task_id'] == task['id'] and not attempts[0]['confirmed_at']
            and not attempts[0]['receipt'] and checkpoint.get('submission_started') is True
            and checkpoint.get('submission_attempt_id') == attempt_id
            and checkpoint.get('prepared_form_hash') == attempts[0]['form_version'] == process['form_version'],
            'Submission history does not match one unconfirmed prepared attempt.')
        started = datetime.fromisoformat(attempts[0]['started_at']).timestamp()
        # Native turn times have second precision; database attempts have microseconds.
        _require(int(started) <= proof['started_at'], 'The stopped turn predates the submission attempt.')
        stopped_events = db.execute("SELECT data FROM wf_process_events WHERE process_id=? "
            "AND type='worker_stopped_confirmed' AND actor='operator'", (process_id,)).fetchall()
        _require(any((event := json.loads(row['data'])).get('turn_id') == proof['turn_id']
            and event.get('thread_id') == proof['thread_id'] and event.get('audit') == str(audit_path)
            and event.get('external_item_count') == 0 and event.get('submission_replayed') is False
            for row in stopped_events), 'No matching durable stopped-worker event exists.')
        store._check_application(db, process)
        store._check_profile_projection(db)
        _require(not db.execute('SELECT 1 FROM wf_questions WHERE process_id=? AND superseded=0 '
            'AND required=1 AND answer IS NULL', (process_id,)).fetchone(), 'Required answers are missing.')
        _require(store.project_consent()['granted'], 'Project browser consent is missing.')
        # Preserve the entire old checkpoint in the event before clearing only
        # its current-attempt markers. Do not modify any historical attempt row.
        stamp = now()
        event = {'attempt_id': attempt_id, 'outcome': 'not_started', 'authorization': authorization,
                 'audit': str(audit_path), 'audit_sha256': digest(audit),
                 'thread_id': proof['thread_id'], 'turn_id': proof['turn_id'],
                 'previous_checkpoint': checkpoint.copy(), 'external_item_count': 0,
                 'submission_replayed': False, 'fresh_preparation_required': True}
        for key in ('submission_started', 'submission_attempt_id', 'prepared_form_hash'):
            checkpoint.pop(key)
        checkpoint['reconciled_submission_attempt_id'] = attempt_id
        db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?', (encode(checkpoint), task['id']))
        store._resume_task(db, task['id'])
        db.execute("UPDATE wf_processes SET submission_status='preparing',block_reason=NULL,"
                   'revision=revision+1,updated_at=? WHERE id=?', (stamp, process_id))
        db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?", (stamp, process_id))
        store._process_event(db, process_id, 'submission_attempt_reconciled', event, 'operator')
        return {'process_id': process_id, 'task_id': task['id'], 'attempt_id': attempt_id,
                'outcome': 'not_started', 'status': 'queued', 'submission_replayed': False}
