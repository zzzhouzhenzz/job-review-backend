"""One durable FIFO for host browser turns, shared by apply and discovery.

Only short file-state changes hold the mutex. A ticket's lifetime lock lets the
host discard dead *waiters*. An active owner is never discarded after a crash:
native work can outlive its host. Clearing that owner needs a separate audited
stopped-worker recovery; this module provides no automatic reset operation.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import time
import uuid

CAPACITY = 1
POLICY = 'chrome_fifo_v1'


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class ChromeAccessGate:
    def __init__(self, run_directory):
        self.directory = Path(run_directory) / '_chrome_access'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ValueError('Chrome access metadata must be private.')
        self.path = self.directory / 'state.json'

    @contextmanager
    def _state(self):
        from codex_transport import _private_json, _replace_private_json
        descriptor = os.open(self.directory / 'state.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(descriptor).st_mode & 0o077:
                raise ValueError('Chrome access lock must be private.')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            value = (_private_json(self.path, max_bytes=1024 * 1024) if self.path.exists() else
                {'version': 1, 'policy': POLICY, 'revision': 0, 'owner': None, 'queue': [], 'blocked_by': []})
            if (value.get('version') != 1 or value.get('policy') != POLICY
                    or not isinstance(value.get('queue'), list)):
                raise ValueError('Chrome access state is invalid.')
            before = repr(value)
            yield value
            if repr(value) != before or not self.path.exists():
                value['revision'] += 1
                _replace_private_json(self.path, value)
        finally:
            os.close(descriptor)

    def _live(self, ticket_id):
        if str(uuid.UUID(ticket_id)) != ticket_id:
            raise ValueError('Chrome ticket identity is invalid.')
        path = self.directory / (ticket_id + '.lock')
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_mode & 0o077:
                raise ValueError('Chrome ticket lock is invalid.')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)

    @staticmethod
    def _public(item):
        if item is None:
            return None
        return {key: item.get(key) for key in ('ticket_id', 'task_id', 'worker_id', 'role',
                'enqueued_at', 'acquired_at', 'started_at', 'state')}

    def snapshot(self):
        # The observer does not prune or clear ownership.
        from codex_transport import _private_json
        value = _private_json(self.path, max_bytes=1024 * 1024) if self.path.exists() else {}
        owner = self._public(value.get('owner'))
        if owner and not self._live(owner['ticket_id']):
            owner['state'] = 'uncertain'
        blocked = value.get('blocked_by', [])
        return {'capacity': CAPACITY, 'policy': POLICY, 'revision': value.get('revision', 0),
            'state': 'uncertain' if owner and owner['state'] == 'uncertain' else
                     'busy' if owner else 'blocked' if blocked else 'free',
            'owner': owner, 'queue': [self._public(item) for item in value.get('queue', [])],
            'blocked_by': blocked}

    def acquire(self, lease, role, closed, heartbeat_interval):
        from codex_transport import CodexTransportError, CodexWorkerStopped, _safe_uuid
        if not _safe_uuid(getattr(lease, 'task_id', None)) or not getattr(lease, 'token', None):
            raise CodexTransportError('Chrome access requires a host task lease.')
        ticket = ChromeTicket(self, lease, role)
        try:
            with self._state() as value:
                from readiness import BrowserRecoverable, BrowserWorkUncertain
                value['queue'] = [item for item in value['queue'] if self._live(item['ticket_id'])]
                owner = value['owner']
                if owner and not self._live(owner['ticket_id']):
                    owner['state'] = 'uncertain'
                existing = value['queue'] + ([value['owner']] if value['owner'] else [])
                for item in existing:
                    if item['task_id'] == lease.task_id or ticket.record['worker_id'] is not None and item['worker_id'] == ticket.record['worker_id']:
                        if item['state'] == 'uncertain':
                            raise BrowserWorkUncertain('Previous Chrome owner has no verified terminal receipt.')
                        raise BrowserRecoverable('browser_session_unavailable')
                value['queue'].append(ticket.record)
            stage = getattr(lease, 'stage', None)
            if callable(stage):
                stage('waiting_for_browser')
            next_heartbeat = 0
            while True:
                if closed.is_set():
                    raise CodexWorkerStopped('Worker stopped while waiting for Chrome.')
                if time.monotonic() >= next_heartbeat:
                    try:
                        if lease.heartbeat() is False:
                            raise RuntimeError('Lease lost.')
                    except Exception:
                        raise CodexTransportError('Task lease was lost while waiting for Chrome.') from None
                    next_heartbeat = time.monotonic() + min(heartbeat_interval, 5)
                blocker = getattr(lease, 'browser_gate_blockers', None)
                blocked = blocker() if callable(blocker) else []
                acquired = False
                with self._state() as value:
                    value['queue'] = [item for item in value['queue'] if self._live(item['ticket_id'])]
                    value['blocked_by'] = blocked
                    owner = value['owner']
                    if owner and not self._live(owner['ticket_id']):
                        owner['state'] = 'uncertain'
                    if not any(item['ticket_id'] == ticket.id for item in value['queue']):
                        raise CodexTransportError('Chrome wait ticket was removed before admission.')
                    if owner is None and not blocked and value['queue'][0]['ticket_id'] == ticket.id:
                        value['queue'].pop(0)
                        ticket.record.update(state='owned', acquired_at=stamp())
                        value['owner'] = ticket.record.copy()
                        ticket.acquired = acquired = True
                if acquired:
                    if callable(stage):
                        stage('submitting' if role == 'application_submit' else 'applying')
                    return ticket
                # No work timeout and no retry: a wait only renews the same lease.
                closed.wait(min(.2, heartbeat_interval))
        except BaseException:
            ticket.finish(safe=True)
            raise


class ChromeTicket:
    def __init__(self, gate, lease, role):
        self.gate, self.id = gate, str(uuid.uuid4())
        self.path = gate.directory / (self.id + '.lock')
        self.lock = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.acquired = self.started = self.safe_release = False
        self.record = {'ticket_id': self.id, 'task_id': lease.task_id,
            'lease_digest': hashlib.sha256(lease.token.encode()).hexdigest(),
            'worker_id': getattr(lease, 'application_worker_id', None), 'role': role,
            'enqueued_at': stamp(), 'state': 'waiting', 'pid': os.getpid()}

    def native_started(self, run):
        with self.gate._state() as value:
            if not value['owner'] or value['owner']['ticket_id'] != self.id:
                raise RuntimeError('Chrome owner changed before native dispatch.')
            value['owner'].update(state='active', run=run.name, started_at=stamp())
            self.record = value['owner'].copy()
            self.started = True

    def verified_terminal(self):
        self.safe_release = True

    def receipt(self):
        return {'capacity': CAPACITY, 'policy': POLICY, 'ticket_id': self.id,
                'acquired_at': self.record.get('acquired_at'), 'started_at': self.record.get('started_at')}

    def finish(self, *, safe):
        if self.lock is None:
            return
        try:
            with self.gate._state() as value:
                value['queue'] = [item for item in value['queue'] if item['ticket_id'] != self.id]
                owner = value['owner']
                if owner and owner['ticket_id'] == self.id:
                    if safe:
                        value['owner'] = None
                    else:
                        owner['state'] = 'uncertain'
        finally:
            os.close(self.lock)
            self.lock = None
            self.path.unlink(missing_ok=True)
