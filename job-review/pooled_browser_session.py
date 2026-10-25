"""Slot-scoped native sessions with explicit legacy affinity and receipt fences.

These records own native identities, not application outcomes. SQLite remains
authoritative. A slot retains its task until that task is terminal; an active
marker is never cleared merely because a thread or task lease expired.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import re

from readiness import BrowserRecoverable, BrowserWorkUncertain

WORKERS = frozenset({'apply-1', 'apply-2', 'apply-3'})


def _lock(path, *, wait=False):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_mode & 0o077:
            raise ValueError('Pool session lock must be private.')
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        raise BrowserRecoverable('browser_session_unavailable') from None
    except BaseException:
        os.close(descriptor)
        raise


def verified_record(directory, record, worker_id):
    from codex_transport import _private_json, _safe_uuid
    if record is None:
        return
    if (not isinstance(record, dict) or record.get('version') != 1
            or record.get('worker_id') != worker_id or not _safe_uuid(record.get('task_id'))):
        raise ValueError('Invalid pooled session binding.')
    if record.get('status') != 'ready':
        raise BrowserWorkUncertain('Pool worker has an unresolved previous native turn.')
    name = record.get('receipt')
    if (not _safe_uuid(record.get('thread_id')) or not isinstance(name, str)
            or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}-[0-9a-f-]{36}', name)
            or (directory / name).is_symlink()):
        raise ValueError('Invalid pooled session receipt reference.')
    receipt = _private_json(directory / name / 'receipt.json')
    browser = record.get('mode') == 'browser'
    if record.get('mode') not in {'browser', 'session_only'}:
        raise ValueError('Invalid pooled session mode.')
    complete = (receipt.get('status') == 'completed' and receipt.get('turn_completed') is True
                and type(receipt.get('exit_code')) is int and receipt['exit_code'] == 0
                and (receipt.get('browser_verified') is True if browser else
                     receipt.get('session_worker') is True and receipt.get('tools') == []
                     and receipt.get('browser_verified') is False))
    stopped_before_turn = (receipt.get('status') in {'failed', 'stopped'}
                          and (receipt.get('failure') or {}).get('safe_before_turn') is True
                          and receipt.get('tools') == [] and not receipt.get('browser_verified'))
    if (not (complete or stopped_before_turn) or receipt.get('transport') != 'app-server'
            or receipt.get('browser') is not browser
            or receipt.get('application_worker_id') != worker_id
            or receipt.get('task_id') != record['task_id']
            or receipt.get('thread_id') != record['thread_id']
            or receipt.get('lease_digest') != record.get('lease_digest')
            or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('lease_digest', '')))):
        raise ValueError('Pool session does not match its host receipt.')
    return receipt


def _terminal(store, task_id):
    with store.transaction() as db:
        task = db.execute('SELECT status FROM wf_tasks WHERE id=?', (task_id,)).fetchone()
        process = db.execute('SELECT submission_status FROM wf_processes WHERE task_id=?', (task_id,)).fetchone()
    return (task is not None and task['status'] in {'succeeded', 'cancelled', 'failed'}
            and (process is None or process['submission_status'] != 'submission_unknown'))


def _reset_allowed(lease, worker_id, thread_id):
    """Accept only an explicit host-verified preparation reset, never model input.

    The caller must derive this marker from its audited migration operation after
    proving the old worker stopped and closing the owned obsolete draft tabs.
    The transport additionally rejects live submission checkpoints/outcomes.
    """
    marker = getattr(lease, 'pool_session_reset', None)
    if not isinstance(marker, dict):
        return False
    if (marker.get('task_id') != lease.task_id or marker.get('worker_id') != worker_id
            or marker.get('legacy_thread_id') != thread_id
            or marker.get('safe_preparation_reset') is not True
            or marker.get('stopped_worker_confirmed') is not True
            or marker.get('owned_tabs_closed') is not True
            or not isinstance(marker.get('evidence_reference'), str)
            or not marker['evidence_reference'].strip()):
        raise ValueError('Invalid preparation reset evidence.')
    import json
    with lease.store.transaction() as db:
        task = db.execute('SELECT * FROM wf_tasks WHERE id=?', (lease.task_id,)).fetchone()
        process = db.execute('SELECT submission_status FROM wf_processes WHERE task_id=?', (lease.task_id,)).fetchone()
    if (task is None or task['kind'] != 'apply' or process is None
            or process['submission_status'] not in {'not_started', 'preparing'}
            or 'checkpoint' in task.keys() and json.loads(task['checkpoint']).get('submission_started')):
        raise BrowserWorkUncertain('An uncertain submission cannot reset its native session.')
    return True


class PooledSession:
    def __init__(self, transport, lease, worker_id, descriptor, value, legacy=None):
        self.transport, self.lease = transport, lease
        self.worker_id, self.lock, self.value, self.legacy = worker_id, descriptor, value, legacy
        self.task_id = lease.task_id
        self.lease_digest = hashlib.sha256(lease.token.encode()).hexdigest()
        self.path = transport.run_directory / '_browser_sessions' / (worker_id + '.json')
        self.previous = copy.deepcopy(value.get('record'))
        self.thread_id = legacy.thread_id if legacy else (self.previous or {}).get('thread_id')
        self.name = 'Job Review ' + worker_id
        self.mode = 'browser'

    def save(self):
        from codex_transport import _replace_private_json
        _replace_private_json(self.path, self.value)

    def binding(self, run, thread_id, status):
        return {'version': 1, 'worker_id': self.worker_id, 'task_id': self.task_id,
                'thread_id': thread_id, 'status': status, 'receipt': run.name,
                'lease_digest': self.lease_digest, 'mode': self.mode}

    def use_session_only(self):
        # Disabling browser plugins avoids intentional browser use, but there is
        # no established guarantee that native resume leaves existing handoff
        # marks intact. Require host-verified transport evidence before doing it.
        has_browser_history = bool(self.legacy or self.value.get('browser_record'))
        if has_browser_history and getattr(self.lease, 'session_only_handoff_verified', False) is not True:
            from codex_transport import SessionOnlyHandoffUnverified
            raise SessionOnlyHandoffUnverified('Session-only resume requires verified browser handoff preservation.')
        self.mode = 'session_only'

    def preservation_notice(self):
        if self.mode == 'session_only':
            return ('\nHost session-only stage: use only the supplied question and answer data. '
                    'Do not call any tool, access Chrome, manipulate tabs, or start helper work.\n')
        if self.legacy and hasattr(self.legacy, 'preservation_notice'):
            return self.legacy.preservation_notice()
        return ('\nHost pool ownership: you are ' + self.worker_id + ' and own only application task '
                + self.task_id + '. Use only this native session\'s verified owned tabs. '
                'Never claim another worker\'s tabs. Before ending every browser turn, markHandoff() '
                'each unfinished draft owned by this task. Revalidate saved tab IDs through the '
                'supported browser API; an old ID alone does not establish ownership.\n')

    def started(self, run):
        if self.legacy:
            self.legacy.started(run)
        self.value['record'] = self.binding(run, self.thread_id, 'active')
        self.value['previous_record'] = self.previous
        self.save()

    def completed(self, run, thread_id, lease):
        if lease.heartbeat() is False:
            raise RuntimeError('Lease lost while saving pool session.')
        record = self.binding(run, thread_id, 'ready')
        verified_record(self.transport.run_directory, record, self.worker_id)
        if self.legacy:
            if self.mode == 'browser':
                self.legacy.completed(run, thread_id, lease)
            else:
                # A verified tool-free turn did not replace browser evidence.
                # Keep the prior browser receipt after the native turn ends.
                self.legacy.failed_before_tools()
        self.value['record'] = record
        if self.mode == 'browser':
            self.value['browser_record'] = record
        self.value.pop('previous_record', None)
        self.save()

    def failed_before_tools(self, *, thread_id=None, run=None):
        from codex_transport import _safe_uuid
        if self.legacy:
            self.legacy.failed_before_tools(thread_id=thread_id, run=run)
        record = self.previous
        if record is None and _safe_uuid(thread_id) and run is not None:
            record = self.binding(run, thread_id, 'ready')
            verified_record(self.transport.run_directory, record, self.worker_id)
        self.value['record'] = record
        self.value.pop('previous_record', None)
        self.save()

    def close(self):
        try:
            if self.legacy:
                self.legacy.close()
        finally:
            os.close(self.lock)


def open_session(transport, lease, role):
    from codex_transport import _BrowserSession, _private_json, _replace_private_json, _safe_uuid
    from shared_browser_session import read_registry, verified_record as verify_legacy
    worker_id = getattr(lease, 'application_worker_id', None)
    if worker_id not in WORKERS or not _safe_uuid(lease.task_id) or not isinstance(lease.token, str) or not lease.token:
        raise ValueError('A pool session requires an assigned worker and task lease.')
    directory = transport.run_directory / '_browser_sessions'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('Pool sessions must be private.')
    descriptor = _lock(directory / (worker_id + '.lock'))
    legacy = None
    try:
        path = directory / (worker_id + '.json')
        value = _private_json(path, max_bytes=8 * 1024 * 1024) if path.exists() or path.is_symlink() else {
            'version': 1, 'worker_id': worker_id, 'task_id': lease.task_id, 'record': None, 'history': []}
        if (value.get('version') != 1 or value.get('worker_id') != worker_id
                or not _safe_uuid(value.get('task_id')) or not isinstance(value.get('history'), list)):
            raise ValueError('Invalid pool session registry.')
        verified_record(transport.run_directory, value.get('record'), worker_id)
        if value.get('record') and value['record']['task_id'] != value['task_id']:
            raise ValueError('Pool session task identity changed.')
        if value.get('browser_record'):
            verified_record(transport.run_directory, value['browser_record'], worker_id)
            if (value['browser_record']['mode'] != 'browser'
                    or value['browser_record']['task_id'] != value['task_id']
                    or not value.get('record')
                    or value['browser_record']['thread_id'] != value['record']['thread_id']):
                raise ValueError('Pool browser evidence belongs to another native session.')
        if value['task_id'] != lease.task_id:
            if not _terminal(lease.store, value['task_id']):
                raise BrowserRecoverable('browser_session_unavailable')
            history = value['history'] + [copy.deepcopy({k: v for k, v in value.items() if k != 'history'})]
            value = {'version': 1, 'worker_id': worker_id, 'task_id': lease.task_id,
                     'record': None, 'history': history}

        # Only the small affinity check is global. Native turns for fresh slots
        # hold separate locks. Existing shared native generations keep their
        # original global lock until they drain or a verified reset is supplied.
        registry_lock = _lock(directory / '_pool_affinity.lock', wait=True)
        legacy_kind = None
        try:
            affinity_path = directory / '_pool_affinity.json'
            affinity = _private_json(affinity_path) if affinity_path.exists() or affinity_path.is_symlink() else {}
            if not isinstance(affinity, dict) or any(value not in WORKERS for value in affinity.values()):
                raise ValueError('Invalid pool native affinity record.')
            task_identity = 'pool-task:' + lease.task_id
            if affinity.get(task_identity) not in (None, worker_id):
                raise BrowserRecoverable('browser_session_unavailable')
            shared_path = directory / '_shared.json'
            shared = read_registry(shared_path) if shared_path.exists() or shared_path.is_symlink() else None
            assigned = next((g for g in reversed(shared['generations']) if lease.task_id in g['tasks']), None) if shared else None
            work_path = directory / (lease.task_id + '.json')
            ready_path = directory / (lease.task_id + '.readiness.json')
            old_record, identity = None, None
            previous_reset = value.get('reset')
            if previous_reset:
                if (not isinstance(previous_reset, dict)
                        or previous_reset.get('task_id') != lease.task_id
                        or previous_reset.get('worker_id') != worker_id
                        or previous_reset.get('safe_preparation_reset') is not True
                        or previous_reset.get('stopped_worker_confirmed') is not True
                        or previous_reset.get('owned_tabs_closed') is not True
                        or not isinstance(previous_reset.get('evidence_reference'), str)
                        or not previous_reset['evidence_reference'].strip()):
                    raise ValueError('Invalid persisted preparation reset evidence.')
                # The retired binding can now be busy with another application.
                # Its later state does not own the reset task's new native turn.
            elif assigned:
                old_record = assigned.get('record')
                identity = 'shared:' + str(assigned['number'])
            elif work_path.exists() or ready_path.exists() or work_path.is_symlink() or ready_path.is_symlink():
                old_record = _private_json(work_path if work_path.exists() or work_path.is_symlink() else ready_path)
                identity = 'task:' + lease.task_id
            reset = bool(previous_reset) or bool(identity and _reset_allowed(lease, worker_id, (old_record or {}).get('thread_id')))
            if identity and not reset:
                # Ordinary continuation still needs a completed legacy receipt.
                # An exact host-audited stopped-owner reset can retire an
                # interrupted active binding while retaining all its evidence.
                verify_legacy(transport.run_directory, old_record)
            if reset and not previous_reset:
                if value.get('record'):
                    value['history'].append(copy.deepcopy({k: v for k, v in value.items() if k != 'history'}))
                value['record'] = None
                value.pop('browser_record', None)
                value['reset'] = copy.deepcopy(lease.pool_session_reset)
                value['legacy_record'] = copy.deepcopy(old_record)
            if identity and not reset:
                owner = affinity.get(identity)
                if owner is not None and owner != worker_id:
                    raise BrowserRecoverable('browser_session_unavailable')
                affinity[identity] = worker_id
                legacy_kind = 'shared' if assigned else 'task'
            affinity[task_identity] = worker_id
            _replace_private_json(affinity_path, affinity)
        finally:
            os.close(registry_lock)
        # Legacy maintenance can inspect an old native thread. Never hold the
        # short global affinity lock during that operation: fresh slots must
        # still be able to open their independent sessions.
        if legacy_kind == 'shared':
            from shared_browser_session import open_session as open_shared
            legacy = open_shared(transport, lease, role)
        elif legacy_kind == 'task':
            legacy = _BrowserSession(transport.run_directory, lease,
                readiness=role == 'linkedin_session' and ready_path.exists())
        _replace_private_json(path, value)
        return PooledSession(transport, lease, worker_id, descriptor, value, legacy)
    except BaseException:
        if legacy:
            legacy.close()
        os.close(descriptor)
        raise
