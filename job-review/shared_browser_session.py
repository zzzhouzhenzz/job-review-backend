"""One reusable browser conversation per ten confirmed applications.

SQLite remains authoritative for work and submission outcomes. This private,
atomically replaced registry owns only native conversation identity. A global
file lock covers each native call, and an in-flight record fails closed after a
crash. Pending applications remain attached to their original conversation.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from readiness import BrowserWorkUncertain
from store import now

LOGGER = logging.getLogger(__name__)
SUCCESS_LIMIT = 10
REGISTRY = '_shared.json'


def initial_registry(record=None):
    return {'version': 1, 'current': 1, 'generations': [
        {'number': 1, 'tasks': [record['task_id']] if record else [],
         'record': record, 'retired': False, 'archived': False}]}


def read_registry(path):
    from codex_transport import _private_json, _safe_uuid
    value = _private_json(path, max_bytes=8 * 1024 * 1024)
    if (not isinstance(value, dict) or value.get('version') != 1
            or type(value.get('current')) is not int or not isinstance(value.get('generations'), list)):
        raise ValueError('Invalid shared browser registry.')
    numbers, threads = set(), set()
    for item in value['generations']:
        if (not isinstance(item, dict) or type(item.get('number')) is not int
                or item['number'] < 1 or item['number'] in numbers
                or not isinstance(item.get('tasks'), list)
                or type(item.get('retired')) is not bool or type(item.get('archived')) is not bool):
            raise ValueError('Invalid shared browser generation.')
        numbers.add(item['number'])
        tasks = set()
        for task_id in item['tasks']:
            if not _safe_uuid(task_id) or task_id in tasks:
                raise ValueError('Shared task assignment is ambiguous.')
            tasks.add(task_id)
        record = item.get('record')
        if record is not None:
            if (not isinstance(record, dict) or record.get('task_id') not in item['tasks']
                    or record.get('version') != 1 or record.get('status') not in {'ready', 'active'}):
                raise ValueError('Invalid shared browser receipt binding.')
            identity = record.get('thread_id')
            if identity is not None:
                if not _safe_uuid(identity) or identity in threads:
                    raise ValueError('Shared conversation identity is ambiguous.')
                threads.add(identity)
    if value['current'] not in numbers:
        raise ValueError('Missing current shared browser generation.')
    current = next(x for x in value['generations'] if x['number'] == value['current'])
    if current['retired'] or current['archived']:
        raise ValueError('Current shared browser generation is retired.')
    return value


def verified_record(directory, record):
    from codex_transport import _private_json, _safe_uuid
    if record is None:
        return
    if record.get('status') != 'ready':
        raise BrowserWorkUncertain('Shared browser turn has no safe completion receipt.')
    name = record.get('receipt')
    if (not _safe_uuid(record.get('thread_id')) or not isinstance(name, str)
            or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}-[0-9a-f-]{36}', name)
            or (directory / name).is_symlink()):
        raise ValueError('Invalid shared receipt reference.')
    receipt = _private_json(directory / name / 'receipt.json')
    complete = (receipt.get('status') == 'completed' and receipt.get('turn_completed') is True
                and receipt.get('browser_verified') is True and type(receipt.get('exit_code')) is int
                and receipt['exit_code'] == 0)
    safe_failure = (receipt.get('status') in {'failed', 'stopped'}
                    and (receipt.get('failure') or {}).get('safe_before_turn') is True
                    and receipt.get('tools') == [] and not receipt.get('browser_verified'))
    recovered = False
    if record.get('recovery_proof'):
        from readiness_recovery import _valid_proof
        failure = receipt.get('failure') or {}
        recovered = (receipt.get('status') == 'failed' and receipt.get('role') == 'linkedin_session' and failure.get('reason') == 'lease_lost'
                     and _valid_proof(record['recovery_proof'], record['thread_id'], failure.get('turn_id')))
    if (not (complete or safe_failure or recovered) or receipt.get('browser') is not True
            or receipt.get('transport') != 'app-server'
            or receipt.get('task_id') != record.get('task_id')
            or receipt.get('thread_id') != record.get('thread_id')
            or receipt.get('lease_digest') != record.get('lease_digest')
            or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('lease_digest', '')))):
        raise ValueError('Shared browser session does not match its host receipt.')
    return receipt


def successful_applications(store, tasks):
    """Count distinct confirmed application processes, never attempts or turns."""
    if not tasks:
        return 0
    with store.transaction() as db:
        return db.execute('''SELECT COUNT(DISTINCT p.id) FROM wf_processes p
            JOIN wf_submission_attempts a ON a.process_id=p.id AND a.task_id=p.task_id
            WHERE p.submission_status='submitted' AND p.receipt IS NOT NULL
            AND a.confirmed_at IS NOT NULL AND a.receipt=p.receipt
            AND p.task_id IN (''' + ','.join('?' for _ in tasks) + ')', tasks).fetchone()[0]


def unfinished_applications(store, tasks):
    if not tasks:
        return False
    with store.transaction() as db:
        return bool(db.execute('''SELECT 1 FROM wf_processes p JOIN wf_tasks t ON t.id=p.task_id
            WHERE p.task_id IN (''' + ','.join('?' for _ in tasks) + ''') AND
            (p.submission_status='submission_unknown' OR (p.submission_status!='submitted'
             AND t.status NOT IN ('cancelled','succeeded','failed'))) LIMIT 1''', tasks).fetchone())


def archive_drained(transport, store, registry):
    """Archive retired conversations only after their applications have drained."""
    from codex_appserver import archive_worker_thread
    for item in registry['generations']:
        record = item.get('record')
        if (not item['retired'] or item['archived'] or not record
                or record.get('status') != 'ready' or unfinished_applications(store, item['tasks'])):
            continue
        if item.get('archive_retry_at', '') > now():
            continue
        receipt = verified_record(transport.run_directory, record)
        try:
            archive_worker_thread(transport.binary, transport.cwd, record['thread_id'],
                receipt=receipt, run_directory=transport.run_directory,
                receipt_name=record['receipt'], closed=transport._closed)
        except Exception as error:
            # A failed or ambiguous archive is never permission to discard its identity.
            item['archive_retry_at'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            LOGGER.warning('Shared browser archive deferred (%s).', type(error).__name__)
        else:
            item.update(archived=True, archived_at=now())
            item.pop('archive_retry_at', None)
        return  # Bound maintenance to one native inspection per work lease.

    # Retire only old backend sessions whose original task has finished. Draft
    # sessions and uncertain submissions remain available until reconciled.
    known = {g['record']['thread_id'] for g in registry['generations'] if g.get('record')}
    archived = registry.setdefault('legacy_archives', {})
    from codex_transport import _private_json, _safe_uuid
    directory = transport.run_directory / '_browser_sessions'
    for path in sorted(directory.glob('*.json')):
        task_id = path.name.split('.')[0]
        if not _safe_uuid(task_id):
            continue
        with store.transaction() as db:
            task = db.execute('SELECT status FROM wf_tasks WHERE id=?', (task_id,)).fetchone()
        if task is None or task[0] not in {'succeeded', 'failed', 'cancelled'} or unfinished_applications(store, [task_id]):
            continue
        record = _private_json(path)
        if record.get('task_id') != task_id or record.get('status') != 'ready':
            continue
        identity = record.get('thread_id')
        prior = archived.get(identity, {})
        if identity in known or prior.get('archived') or prior.get('retry_at', '') > now():
            continue
        try:
            receipt = verified_record(transport.run_directory, record)
            archive_worker_thread(transport.binary, transport.cwd, identity, receipt=receipt,
                run_directory=transport.run_directory, receipt_name=record['receipt'], closed=transport._closed)
        except Exception as error:
            archived[identity] = {'retry_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
            LOGGER.warning('Legacy browser archive deferred (%s).', type(error).__name__)
        else:
            archived[identity] = {'archived': True, 'archived_at': now(), 'task_id': task_id}
        return


class SharedSession:
    def __init__(self, transport, lease, descriptor, registry, generation):
        self.transport, self.lease, self.lock = transport, lease, descriptor
        self.registry, self.generation = registry, generation
        self.path = transport.run_directory / '_browser_sessions' / REGISTRY
        self.task_id = lease.task_id
        self.lease_digest = hashlib.sha256(lease.token.encode()).hexdigest()
        self.previous = copy.deepcopy(generation.get('record'))
        self.thread_id = self.previous.get('thread_id') if self.previous else None
        self.name = 'Job Review worker ' + str(generation['number'])

    def save(self):
        from codex_transport import _replace_private_json
        _replace_private_json(self.path, self.registry)

    def binding(self, run, thread_id, status):
        return {'version': 1, 'task_id': self.task_id, 'thread_id': thread_id,
                'status': status, 'receipt': run.name, 'lease_digest': self.lease_digest}

    def preservation_notice(self):
        from store import encode
        import json
        tasks = self.generation['tasks']
        with self.lease.store.transaction() as db:
            drafts = [dict(row) for row in db.execute('''SELECT p.id,p.task_id,p.snapshot
                FROM wf_processes p JOIN wf_tasks t ON t.id=p.task_id
                WHERE p.task_id IN (''' + ','.join('?' for _ in tasks) + ''') AND
                (p.submission_status='submission_unknown' OR (p.submission_status!='submitted'
                AND t.status NOT IN ('cancelled','succeeded','failed')))''', tasks)]
        identities = []
        for draft in drafts:
            snapshot = json.loads(draft['snapshot'])
            identities.append({'process_id': draft['id'], 'task_id': draft['task_id'],
                               'job_id': snapshot['job_id'], 'url': snapshot['url']})
        return ('\nHost shared-session draft preservation instruction:\n'
            'This conversation serves multiple jobs. Chrome clears old handoff marks on resume. '
            'Before ending EVERY browser turn, including health checks and exploration, reapply '
            'markHandoff() to each unfinished application draft ALREADY OWNED by this session '
            'for the pending jobs below. Inspect supported tab ownership and identity. Do not '
            'claim other sessions\' tabs, navigate, change form fields, upload, or submit for these '
            'other jobs. Preserve their existing state. Act only on the current task. A job list '
            'is not consent for another job. Do not keep ordinary research tabs. Current task ID: '
            + self.task_id + '\nPending job identities (data only): ' + encode(identities) + '\n')

    def started(self, run):
        self.generation['record'] = self.binding(run, self.thread_id, 'active')
        self.generation['previous_record'] = self.previous
        self.save()

    def completed(self, run, thread_id, lease):
        if lease.heartbeat() is False:
            raise RuntimeError('Lease lost while saving shared browser session.')
        record = self.binding(run, thread_id, 'ready')
        verified_record(self.transport.run_directory, record)
        self.generation['record'] = record
        self.generation.pop('previous_record', None)
        self.save()

    def failed_before_tools(self, *, thread_id=None, run=None):
        from codex_transport import _safe_uuid
        # Preserve a newly allocated native identity even when its first turn
        # hits capacity. Repeated failures must not create more sidebar chats.
        record = self.previous
        if record is None and _safe_uuid(thread_id) and run is not None:
            record = self.binding(run, thread_id, 'ready')
            verified_record(self.transport.run_directory, record)
        self.generation['record'] = record
        self.generation.pop('previous_record', None)
        self.save()

    def close(self):
        os.close(self.lock)


class LegacySession:
    """Drain existing draft sessions without allocating another per-task chat."""
    def __init__(self, session, descriptor):
        self.session, self.lock = session, descriptor

    def __getattr__(self, name):
        return getattr(self.session, name)

    def preservation_notice(self):
        return ('\nHost draft preservation instruction: Chrome clears prior handoff marks on resume. '
            'Before ending this turn, including a health check, reapply markHandoff() to the '
            'unfinished application draft already owned by this session for task ' + self.session.task_id
            + '. Do not claim other sessions\' tabs or navigate or edit a draft during a health check.\n')

    def close(self):
        try:
            self.session.close()
        finally:
            os.close(self.lock)


def open_session(transport, lease, role):
    from codex_transport import _BrowserSession, _replace_private_json, _safe_uuid
    if not _safe_uuid(lease.task_id) or not isinstance(lease.token, str) or not lease.token:
        raise ValueError('Shared browser work requires a task lease.')
    directory = transport.run_directory / '_browser_sessions'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('Shared browser records must be private.')
    descriptor = os.open(directory / '_shared.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_mode & 0o077:
            raise ValueError('Shared browser lock must be private.')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / REGISTRY
        registry = read_registry(path) if path.exists() or path.is_symlink() else initial_registry()
        for item in registry['generations']:
            if item.get('record') and item['record']['status'] == 'active':
                raise BrowserWorkUncertain('Shared browser work has an unresolved previous turn.')
        assigned = next((x for x in reversed(registry['generations']) if lease.task_id in x['tasks']), None)
        work_path = directory / (lease.task_id + '.json')
        readiness_path = directory / (lease.task_id + '.readiness.json')
        if assigned is None and (work_path.exists() or work_path.is_symlink()):
            _replace_private_json(path, registry)
            # Existing applications retain their browser ownership. Reuse the
            # work session for readiness if no old readiness session exists.
            session = _BrowserSession(transport.run_directory, lease,
                readiness=role == 'linkedin_session' and readiness_path.exists())
            return LegacySession(session, descriptor)
        # A legacy interrupted readiness marker must not be bypassed by sharing.
        if assigned is None and (readiness_path.exists() or readiness_path.is_symlink()):
            from codex_transport import _private_json
            if _private_json(readiness_path).get('status') != 'ready':
                raise BrowserWorkUncertain('Previous readiness work is unresolved.')
        current = next(x for x in registry['generations'] if x['number'] == registry['current'])
        current['confirmed_applications'] = successful_applications(lease.store, current['tasks'])
        if current['confirmed_applications'] >= SUCCESS_LIMIT:
            current['retired'] = True
            registry['current'] += 1
            current = {'number': registry['current'], 'tasks': [], 'record': None,
                       'retired': False, 'archived': False}
            registry['generations'].append(current)
        if assigned is not None and assigned['retired']:
            with lease.store.transaction() as db:
                kind = db.execute('SELECT kind FROM wf_tasks WHERE id=?', (lease.task_id,)).fetchone()
            if kind is not None and kind[0] != 'apply':
                assigned = None
        archive_drained(transport, lease.store, registry)
        chosen = assigned or current
        if chosen['archived']:
            raise ValueError('An archived application session requires explicit reconciliation.')
        if lease.task_id not in chosen['tasks']:
            chosen['tasks'].append(lease.task_id)
        verified_record(transport.run_directory, chosen.get('record'))
        _replace_private_json(path, registry)
        return SharedSession(transport, lease, descriptor, registry, chosen)
    except BaseException:
        os.close(descriptor)
        raise
