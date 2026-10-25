"""Task-scoped user guidance. Text never clears a gate or authorizes a replay."""
from __future__ import annotations

import json
import uuid

from store import ReviewError, digest, now, text
from application_pool import browser_resource


UNBLOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_unblock_instructions (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 task_id TEXT NOT NULL REFERENCES wf_tasks(id),
 process_id TEXT REFERENCES wf_processes(id), process_revision INTEGER,
 task_updated_at TEXT NOT NULL, binding_hash TEXT NOT NULL,
 mode TEXT NOT NULL, text TEXT NOT NULL, wake_status TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wf_unblock_task ON wf_unblock_instructions(task_id,created_at,id);
"""

HOST_INSTRUCTION_BOUNDARY = (
    "These are user instructions for this exact existing task. They do not replace "
    "structured question answers, grant new employer legal consent, change job approval, "
    "override browser/tool permission, or authorize a new submission attempt. "
    "Verify any claimed completed action from fresh evidence. Keep all lease, blocker, "
    "required-answer and submission checks. An uncertain submission permits only "
    "reconciliation; never replay it based on this text."
)
PREPARATION_RETRY_REASONS = frozenset({'tool_approval_required', 'file_access_required',
    'browser_action_required', 'application_action_required', 'application_page_transient'})


class UnblockInstructionsMixin:
    @staticmethod
    def _instruction_revision_current(db, row, process):
        if row['process_revision'] == (process['revision'] if process else None):
            return True
        if (not process or row['mode'] != 'preparation_retry'
                or process['revision'] != row['process_revision'] + 1
                or process['submission_status'] != 'preparing' or process['block_reason'] is not None):
            return False
        events = db.execute('SELECT type,data FROM wf_process_events WHERE process_id=? ORDER BY id DESC LIMIT 2',
                            (process['id'],)).fetchall()
        # Only the ordinary start of this requested attempt can carry guidance
        # across its one revision change. Answers/new blockers cannot do so.
        return (len(events) == 2 and events[0]['type'] == 'preparing'
            and json.loads(events[0]['data']).get('task_id') == row['task_id']
            and events[1]['type'] == 'unblock_instruction_saved'
            and json.loads(events[1]['data']).get('instruction_id') == row['id'])

    def _instruction_binding(self, db, task, process):
        if process:
            event = db.execute("""SELECT id FROM wf_process_events WHERE process_id=?
                AND type IN ('blocked','questions_requested','submission_started')
                ORDER BY id DESC LIMIT 1""", (process['id'],)).fetchone()
            return digest({'task_id': task['id'], 'payload': task['payload'],
                'process_id': process['id'], 'decision_id': process['decision_id'],
                'snapshot': process['snapshot'], 'form_version': process['form_version'],
                'blocker_event_id': event['id'] if event else None})
        return digest({'task_id': task['id'], 'payload': task['payload'],
                       'blocker': task['error']})

    def _instruction_target(self, db, task_id):
        task = self._task(db, task_id)
        process_id = json.loads(task['payload']).get('process_id') if task['kind'] == 'apply' else None
        process = self._process(db, process_id) if process_id else None
        target = {'task_id': task_id, 'task_updated_at': task['updated_at'],
            'task_status': task['status'], 'process_id': process_id,
            'process_revision': process['revision'] if process else None,
            'block_reason': (process['block_reason'] if process else None) or task['error'],
            'can_submit': False, 'mode': 'unavailable', 'unavailable_reason': None}
        if task['kind'] not in {'apply', 'explore'} or task['status'] in {'succeeded', 'cancelled'}:
            target['unavailable_reason'] = 'task_closed'
        elif task['status'] == 'running' or task['lease_token']:
            target['unavailable_reason'] = 'task_busy'
        elif task['status'] not in {'waiting_for_user', 'failed'} or not target['block_reason']:
            target['unavailable_reason'] = 'task_not_blocked'
        elif process and process['submission_status'] == 'submitted':
            target['unavailable_reason'] = 'task_closed'
        else:
            uncertain = (task['error'] == 'submission_unknown'
                or json.loads(task['checkpoint']).get('submission_started')
                or process and (process['submission_status'] == 'submission_unknown'
                    or db.execute('SELECT 1 FROM wf_submission_attempts WHERE process_id=?',
                                  (process_id,)).fetchone()))
            if not uncertain and process:
                try:
                    self._check_application(db, process)
                except ReviewError:
                    target['unavailable_reason'] = 'authorization_changed'
                    return task, process, target
            if process and process['task_id'] != task_id:
                target['unavailable_reason'] = 'stale_revision'
                return task, process, target
            target.update(can_submit=True, mode='reconciliation_only' if uncertain else 'manual_review')
            if not uncertain:
                missing = bool(process and db.execute('''SELECT 1 FROM wf_questions
                    WHERE process_id=? AND superseded=0 AND required=1 AND answer IS NULL''',
                    (process_id,)).fetchone())
                if missing or target['block_reason'] == 'questions_pending':
                    target['mode'] = 'questions_pending' if target['block_reason'] == 'questions_pending' else 'manual_review'
                elif (process and task['status'] == 'waiting_for_user'
                        and task['error'] in PREPARATION_RETRY_REASONS
                        and browser_resource(task['resource'])
                        and task['failure_count'] < task['max_attempts']
                        and process['submission_status'] == 'preparing'
                        and process['block_reason'] in (None, task['error'])
                        and not task['resource_quarantined']
                        and not db.execute("""SELECT 1 FROM wf_resource_locks WHERE task_id=?
                            OR (resource='browser' AND quarantined=1)""", (task_id,)).fetchone()
                        and self._project_consent(db)['granted']):
                    target['mode'] = 'preparation_retry'
        return task, process, target

    def unblock_instructions(self, task_id):
        """Public read snapshot; never exposes task payloads or lease tokens."""
        task_id = text(task_id, 'Task ID', 200, required=True)
        with self.transaction() as db:
            task, process, target = self._instruction_target(db, task_id)
            binding = self._instruction_binding(db, task, process)
            rows = db.execute('SELECT * FROM wf_unblock_instructions WHERE task_id=? ORDER BY created_at,id',
                              (task_id,)).fetchall()
            latest = rows[-1]['id'] if rows else None
            history = []
            for row in rows:
                current = (row['binding_hash'] == binding
                    and self._instruction_revision_current(db, row, process)
                    and task['status'] not in {'succeeded', 'cancelled'}
                    and target['unavailable_reason'] not in {'authorization_changed', 'task_closed'})
                status = 'current' if current and row['id'] == latest else 'superseded' if current else 'stale'
                history.append({key: row[key] for key in ('id', 'request_id', 'task_id', 'process_id',
                    'process_revision', 'task_updated_at', 'text', 'mode', 'wake_status', 'created_at')})
                history[-1]['status'] = status
            return {'target': target, 'instructions': history}

    def save_unblock_instruction(self, payload):
        if not isinstance(payload, dict):
            raise ReviewError('The instruction command must be an object.')
        fields = {'request_id', 'task_id', 'task_updated_at', 'process_id', 'process_revision', 'text'}
        if set(payload) != fields:
            raise ReviewError('Provide exactly the instruction command fields.')
        request_id = text(payload['request_id'], 'Request ID', 200, required=True)
        task_id = text(payload['task_id'], 'Task ID', 200, required=True)
        content = text(payload['text'], 'Instruction', 4000, required=True)
        text(payload['task_updated_at'], 'Task timestamp', 100, required=True)
        command = {'command': 'unblock_instruction', 'payload': payload}
        with self.transaction(True) as db:
            previous = self._replay(db, request_id, command)
            if previous:
                return previous
            task, process, target = self._instruction_target(db, task_id)
            if target['unavailable_reason'] == 'task_busy':
                raise ReviewError('The task is active. Refresh after it stops.', 409, reason='task_busy')
            if (payload['task_updated_at'] != task['updated_at']
                    or payload['process_id'] != target['process_id']
                    or payload['process_revision'] != target['process_revision']
                    or process and type(payload['process_revision']) is not int):
                raise ReviewError('The task changed. Refresh before sending instructions.', 409,
                                  reason='stale_revision')
            if not target['can_submit']:
                raise ReviewError('This task cannot accept unblock instructions.', 409,
                                  reason=target['unavailable_reason'])
            stamp, instruction_id = now(), str(uuid.uuid4())
            wake = 'saved_only'
            if target['mode'] == 'preparation_retry':
                self._resume_task(db, task_id)
                db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?", (stamp, process['id']))
                wake = 'retry_scheduled'
            db.execute('INSERT INTO wf_unblock_instructions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (instruction_id, request_id, task_id, target['process_id'], target['process_revision'],
                 task['updated_at'], self._instruction_binding(db, task, process), target['mode'], content, wake, stamp))
            db.execute('UPDATE wf_tasks SET updated_at=? WHERE id=?', (stamp, task_id))
            if process:
                self._process_event(db, process['id'], 'unblock_instruction_saved',
                    {'instruction_id': instruction_id, 'mode': target['mode'], 'wake_status': wake}, 'user')
            receipt = {'request_id': request_id, 'accepted': True, 'instruction_id': instruction_id,
                'task_ids': [task_id], 'process_ids': [process['id']] if process else [],
                'mode': target['mode'], 'wake_status': wake, 'task_updated_at': stamp}
            return self._save_receipt(db, request_id, command, receipt)

    def unblock_instruction_context(self, task_id, token, *, purpose):
        """Read current guidance under the caller's live task lease; no state writes."""
        if purpose not in {'readiness', 'application', 'exploration', 'reconciliation'}:
            raise ReviewError('Unsupported instruction context purpose.')
        with self.transaction() as db:
            task = self._leased_task(db, task_id, token)
            process = (self._process(db, json.loads(task['payload'])['process_id'])
                       if task['kind'] == 'apply' else None)
            row = db.execute('SELECT * FROM wf_unblock_instructions WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
                             (task_id,)).fetchone()
            if (not row or row['binding_hash'] != self._instruction_binding(db, task, process)
                    or not self._instruction_revision_current(db, row, process)):
                return None
            uncertain = (json.loads(task['checkpoint']).get('submission_started')
                or process and (process['submission_status'] in {'submission_unknown', 'submitted'}
                    or db.execute('SELECT 1 FROM wf_submission_attempts WHERE process_id=?', (process['id'],)).fetchone()))
            if process and process['submission_status'] == 'submitted':
                return None
            if (uncertain or row['mode'] == 'reconciliation_only') and purpose != 'reconciliation':
                return None
            if process and not uncertain:
                self._check_application(db, process)
            if task['kind'] not in {'apply', 'explore'} or task['resource_quarantined']:
                return None
            return {'instruction_id': row['id'], 'task_id': task_id, 'process_id': row['process_id'],
                    'text': row['text'], 'created_at': row['created_at'], 'mode': row['mode'],
                    'authority': 'user', 'boundary': HOST_INSTRUCTION_BOUNDARY}
