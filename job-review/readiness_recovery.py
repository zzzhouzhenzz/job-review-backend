"""Recover an interrupted native readiness turn only when it did no external work.

This path never operates Chrome. A terminal native record with every item loaded
must prove that no tool, command, or helper started. Application turns and possible
submissions cannot use this path. A private journal makes file/SQLite repair safe
to repeat after a crash between the two writes.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from store import ReviewError, encode, now


LOGGER = logging.getLogger(__name__)
MAX_RECOVERIES = 3


def _valid_proof(proof, thread_id, turn_id):
    return (isinstance(proof, dict) and proof.get('version') == 1
            and proof.get('thread_id') == thread_id and proof.get('turn_id') == turn_id
            and proof.get('turn_status') == 'interrupted' and proof.get('items_view') == 'full'
            and proof.get('stable_snapshots') == 2 and proof.get('inspector_exit_code') == 0
            and type(proof.get('external_item_count')) is int and proof['external_item_count'] == 0
            and type(proof.get('child_count')) is int and proof['child_count'] == 0)


def recover_interrupted_readiness(store, transport):
    from codex_appserver import inspect_inactive_turn
    from codex_transport import _private_json, _replace_private_json, _safe_uuid

    if getattr(transport, 'browser_transport', 'exec') != 'app-server' or transport._closed.is_set():
        return None
    with store.transaction() as db:
        candidate = db.execute("""SELECT t.id,t.updated_at,l.lease_token FROM wf_tasks t
            JOIN wf_resource_locks l ON l.task_id=t.id
            WHERE l.resource='browser' AND l.quarantined=1
            AND t.status='waiting_for_user' AND t.error='worker_lease_lost'
            AND t.kind IN ('apply','explore')""").fetchone()
    if candidate is None:
        return None
    task_id, expected_update, token = candidate
    if not _safe_uuid(task_id):
        return None
    directory = transport.run_directory / '_browser_sessions'
    if not directory.is_dir() or directory.is_symlink() or directory.stat().st_mode & 0o077:
        return None
    shared = getattr(transport, 'browser_session_policy', 'per_task') == 'shared'
    descriptor = os.open(directory / ('_shared.lock' if shared else task_id + '.lock'),
                         os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    legacy_descriptor = None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None  # The old transport still owns its receipt/session writes.
        if os.fstat(descriptor).st_mode & 0o077:
            return None
        path = directory / (task_id + '.readiness.json')
        journal = directory / (task_id + '.readiness-recovery.json')
        registry, generation = None, None
        preserve_legacy = False
        if shared:
            from shared_browser_session import read_registry, REGISTRY
            shared_path = directory / REGISTRY
            if shared_path.exists() or shared_path.is_symlink():
                registry = read_registry(shared_path)
                generation = next((g for g in reversed(registry['generations'])
                                   if g.get('record') and g['record']['task_id'] == task_id
                                   and (g['record']['status'] == 'active'
                                        or g['record'].get('recovery_proof'))), None)
            if generation is None:
                legacy_descriptor = os.open(directory / (task_id + '.lock'),
                    os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(legacy_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return None
                if os.fstat(legacy_descriptor).st_mode & 0o077:
                    return None
                if not path.exists() and not path.is_symlink():
                    application_path = directory / (task_id + '.json')
                    if application_path.exists() or application_path.is_symlink():
                        path, preserve_legacy = application_path, True
        if generation is not None:
            binding = generation['record']
            if binding['status'] == 'ready':
                recorded = _private_json(journal)
                if recorded.get('proof') != binding.get('recovery_proof'):
                    return None
                binding = recorded.get('binding', {})
        elif path.exists() or path.is_symlink():
            binding = _private_json(path)
        elif journal.exists():
            recorded = _private_json(journal)
            if not isinstance(recorded, dict) or recorded.get('version') != 1:
                return None
            binding = recorded.get('binding', {})
        else:
            return None
        if preserve_legacy and binding.get('status') == 'ready' and binding.get('recovery_proof'):
            recorded = _private_json(journal)
            if recorded.get('proof') != binding['recovery_proof']:
                return None
            binding = recorded.get('binding', {})
        lease_digest = hashlib.sha256(token.encode()).hexdigest()
        if (not isinstance(binding, dict) or binding.get('version') != 1 or binding.get('task_id') != task_id
                or binding.get('status') != 'active' or binding.get('lease_digest') != lease_digest):
            return None
        run_name = binding.get('receipt')
        if not isinstance(run_name, str) or not re.fullmatch(r'linkedin_session-[0-9a-f-]{36}', run_name):
            return None
        run = transport.run_directory / run_name
        if run.is_symlink():
            return None
        receipt = _private_json(run / 'receipt.json')
        if not isinstance(receipt, dict):
            return None
        failure = receipt.get('failure') or {}
        if not isinstance(failure, dict):
            return None
        thread_id, turn_id = receipt.get('thread_id'), failure.get('turn_id')
        if (receipt.get('role') != 'linkedin_session' or receipt.get('transport') != 'app-server'
                or receipt.get('status') != 'failed' or receipt.get('task_id') != task_id
                or receipt.get('lease_digest') != lease_digest or failure.get('reason') != 'lease_lost'
                or not _safe_uuid(thread_id) or not _safe_uuid(turn_id)
                or binding.get('thread_id') not in (None, thread_id)):
            return None
        application = directory / (task_id + '.json')
        if application != path and (application.exists() or application.is_symlink()):
            other = _private_json(application)
            if not isinstance(other, dict) or other.get('task_id') != task_id or other.get('status') != 'ready':
                return None
        proof = inspect_inactive_turn(binary=transport.binary, cwd=transport.cwd,
                                      thread_id=thread_id, turn_id=turn_id, closed=transport._closed)
        if not _valid_proof(proof, thread_id, turn_id) or transport._closed.is_set():
            return None
        with store.transaction(True) as db:
            current = store._task(db, task_id)
            lock = db.execute("SELECT * FROM wf_resource_locks WHERE resource='browser'").fetchone()
            if (current['updated_at'] != expected_update or current['error'] != 'worker_lease_lost'
                    or current['status'] != 'waiting_for_user' or not current['resource_quarantined']
                    or lock is None or lock['task_id'] != task_id or lock['lease_token'] != token
                    or not store._project_consent(db)['granted']):
                return None
            checkpoint = json.loads(current['checkpoint'])
            count = checkpoint.get('readiness_recovery_count', 0)
            if type(count) is not int or not 0 <= count <= MAX_RECOVERIES or checkpoint.get('submission_started'):
                return None
            process = None
            if current['kind'] == 'apply':
                process = store._process(db, json.loads(current['payload'])['process_id'])
                if process['submission_status'] not in ('not_started', 'preparing'):
                    return None
                store._check_application(db, process)
                if db.execute('SELECT 1 FROM wf_submission_attempts WHERE process_id=?', (process['id'],)).fetchone():
                    return None
                if db.execute("""SELECT 1 FROM wf_questions WHERE process_id=? AND superseded=0
                    AND required=1 AND answer IS NULL""", (process['id'],)).fetchone():
                    return None
            # The native proof and original binding survive a failed DB commit.
            # On retry, the same quarantined lock token must still match them.
            _replace_private_json(journal, {'version': 1, 'binding': binding, 'proof': proof, 'checked_at': now()})
            if generation is not None:
                generation['record'] = dict(binding, status='ready', thread_id=thread_id, recovery_proof=proof)
                generation.pop('previous_record', None)
                _replace_private_json(shared_path, registry)
            elif preserve_legacy:
                _replace_private_json(path, dict(binding, status='ready', thread_id=thread_id, recovery_proof=proof))
            else:
                path.unlink(missing_ok=True)
            checkpoint['readiness_recovery_count'] = count + 1
            db.execute('DELETE FROM wf_resource_locks WHERE task_id=? AND lease_token=?', (task_id, token))
            # Credit only this no-action lease failure. All earlier failures stay.
            failures = max(0, current['failure_count'] - 1)
            db.execute('UPDATE wf_tasks SET checkpoint=?,failure_count=? WHERE id=?',
                       (encode(checkpoint), failures, task_id))
            retry = count < MAX_RECOVERIES and failures < current['max_attempts']
            available = (datetime.now(timezone.utc) + timedelta(seconds=5 * 2 ** count)).isoformat(timespec='microseconds')
            result = store._release_task(db, task_id, 'retry_wait' if retry else 'failed',
                error='readiness_interrupted' if retry else 'readiness_recovery_exhausted', available_at=available)
            if process:
                db.execute('UPDATE wf_processes SET block_reason=?,revision=revision+1,updated_at=? WHERE id=?',
                           (None if retry else 'readiness_recovery_exhausted', now(), process['id']))
                db.execute('UPDATE queue SET status=?,updated_at=? WHERE id=?',
                           ('queued' if retry else 'needs_input', now(), process['id']))
                store._process_event(db, process['id'], 'readiness_recovered',
                    {'reason': 'terminal_turn_without_external_work', 'recovery_count': count + 1,
                     'evidence_reference': str(journal), 'retry_at': available if retry else None}, 'health_monitor')
            return result
    except (OSError, ValueError, KeyError, TypeError, ReviewError) as error:
        LOGGER.warning('Readiness recovery could not verify state (error=%s).', type(error).__name__)
        return None
    finally:
        if legacy_descriptor is not None:
            os.close(legacy_descriptor)
        os.close(descriptor)
