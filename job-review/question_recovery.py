"""A browser-free lane rechecks parked questions when applicant evidence changes.

Only hashes, counters and safe error codes are stored here. Answer authority and
source validation remain in the normal question reviewer and answer writer.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
import uuid

from store import ReviewError, digest


LOGGER = logging.getLogger(__name__)
QUESTION_RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_question_reviews (
 review_key TEXT PRIMARY KEY,
 process_id TEXT NOT NULL REFERENCES wf_processes(id),
 context_hash TEXT NOT NULL, evidence_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','completed','retry_wait','exhausted','obsolete')),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at REAL NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_until REAL,
 last_error TEXT, answered_count INTEGER NOT NULL DEFAULT 0,
 unresolved_count INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


def context_hash(context):
    """Ignore timestamps; include every decision, field, binding and revision."""
    value = copy.deepcopy(context)
    value['process'].pop('updated_at', None)
    for question in value['questions']:
        question.pop('updated_at', None)
    return digest(value)


def review_key(context, evidence):
    return digest({'context': context_hash(context), 'evidence': evidence.hash})


class RecoveryLease:
    def __init__(self, runner, store, context, evidence, key, owner):
        # Import lazily: the operator module imports WorkflowStore at startup.
        from reconcile_questions import ReviewLease
        self.runner, self.store = runner, store
        self.original = ReviewLease(store, context, evidence)
        self.task_id = self.original.task_id
        self.key, self.owner = key, owner

    def heartbeat(self):
        if self.runner.stopped.is_set():
            raise ReviewError('Question recovery is stopping.', 503, reason='review_stopped')
        self.original.heartbeat()
        stamp = self.runner.clock()
        with self.store.transaction(True) as db:
            changed = db.execute("""UPDATE wf_question_reviews SET lease_until=?,updated_at=?
                WHERE review_key=? AND status='running' AND lease_owner=? AND lease_until>?""",
                (stamp + self.runner.lease_seconds, stamp, self.key, self.owner, stamp))
            if changed.rowcount != 1:
                raise ReviewError('Question review ownership changed.', 409, reason='review_lease_lost')
        return True


class QuestionRecovery:
    """Schedule at most one review lane without holding an HTTP/worker thread."""
    def __init__(self, runtime, *, poll_interval=5, retry_delays=(30, 120),
                 lease_seconds=60, clock=time.time):
        self.runtime, self.clock = runtime, clock
        self.poll_interval, self.retry_delays = poll_interval, tuple(retry_delays)
        self.lease_seconds = lease_seconds
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.running = False
        self.answers_dirty = False
        self.next_poll = 0
        self.last_scan_error = None

    def schedule(self, store):
        with self.lock:
            if (self.stopped.is_set() or self.clock() < self.next_poll
                    or self.running):
                return 0
            self._start_locked(store)
        return 0

    def answers_changed(self, store):
        """Wake after a committed user answer; coalesce changes during a scan."""
        with self.lock:
            if self.stopped.is_set():
                return 0
            if self.running:
                self.answers_dirty = True
            else:
                self._start_locked(store)
        return 0

    def _start_locked(self, store):
        self.next_poll = self.clock() + self.poll_interval
        self.answers_dirty = False
        self.thread = threading.Thread(target=self._background, args=(store,),
                                       name='question-recovery', daemon=False)
        self.running = True
        try:
            self.thread.start()
        except Exception:
            self.running = False
            raise

    def close(self):
        self.stopped.set()

    def join(self, timeout=5):
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                from worker_runner import WorkerShutdownTimeout
                raise WorkerShutdownTimeout('Question recovery remains active; retain the database lock.')

    def _background(self, store):
        while True:
            failed = False
            try:
                self.run_pending(store)
                self.last_scan_error = None
            except Exception as error:
                failed = True
                code = type(error).__name__
                if code != self.last_scan_error:
                    LOGGER.warning('Question recovery scan could not finish (error=%s).', code)
                    self.last_scan_error = code
            with self.lock:
                if self.answers_dirty and not self.stopped.is_set() and not failed:
                    self.answers_dirty = False
                    continue
                self.next_poll = self.clock() + (max(self.poll_interval, 30) if failed else self.poll_interval)
                self.running = False
                return

    def _claim(self, store, context, evidence):
        key, stamp, owner = review_key(context, evidence), self.clock(), str(uuid.uuid4())
        with store.transaction(True) as db:
            previous = db.execute('SELECT * FROM wf_question_reviews WHERE review_key=?', (key,)).fetchone()
            if previous:
                if (previous['status'] in ('completed', 'exhausted', 'obsolete')
                        or previous['status'] == 'running' and previous['lease_until'] > stamp
                        or previous['next_attempt_at'] > stamp):
                    return None
                if previous['attempts'] >= len(self.retry_delays) + 1:
                    db.execute("""UPDATE wf_question_reviews SET status='exhausted',
                        last_error='review_interrupted',lease_owner=NULL,lease_until=NULL,updated_at=?
                        WHERE review_key=?""", (stamp, key))
                    return None
                db.execute("""UPDATE wf_question_reviews SET status='running',attempts=attempts+1,
                    lease_owner=?,lease_until=?,updated_at=? WHERE review_key=?""",
                    (owner, stamp + self.lease_seconds, stamp, key))
            else:
                db.execute("""INSERT INTO wf_question_reviews
                    (review_key,process_id,context_hash,evidence_hash,status,attempts,
                     lease_owner,lease_until,created_at,updated_at) VALUES (?,?,?,?,'running',1,?,?,?,?)""",
                    (key, context['process']['id'], context_hash(context), evidence.hash,
                     owner, stamp + self.lease_seconds, stamp, stamp))
        return key, owner

    def _failed(self, store, key, owner, error):
        stamp = self.clock()
        stale = (isinstance(error, ReviewError) and error.status == 409
                 and error.reason != 'review_lease_lost')
        code = 'review_state_changed' if stale else type(error).__name__
        with store.transaction(True) as db:
            row = db.execute("SELECT * FROM wf_question_reviews WHERE review_key=? AND lease_owner=?",
                             (key, owner)).fetchone()
            if row is None:
                return
            retry = not stale and row['attempts'] <= len(self.retry_delays)
            status = 'obsolete' if stale else 'retry_wait' if retry else 'exhausted'
            delay = self.retry_delays[row['attempts'] - 1] if retry else 0
            attempts = row['attempts']
            db.execute("""UPDATE wf_question_reviews SET status=?,last_error=?,next_attempt_at=?,attempts=?,
                lease_owner=NULL,lease_until=NULL,updated_at=? WHERE review_key=? AND lease_owner=?""",
                (status, code, stamp + delay, attempts, stamp, key, owner))
        if not stale:
            LOGGER.warning('Question review failed (process=%s, error=%s, state=%s).',
                           row['process_id'], code, status)

    def _completed(self, store, key, owner, context, evidence, answers, unresolved):
        stamp = self.clock()
        with store.transaction(True) as db:
            changed = db.execute("""UPDATE wf_question_reviews SET status='completed',
                answered_count=?,unresolved_count=?,last_error=NULL,lease_owner=NULL,lease_until=NULL,
                updated_at=? WHERE review_key=? AND lease_owner=? AND status='running'""",
                (len(answers), len(unresolved), stamp, key, owner))
            if not changed.rowcount or not answers or not unresolved:
                return
            # Saving supported answers increments the process exactly once. The
            # untouched remainder was already reviewed. Record that exact next
            # state, not whatever a concurrent phone answer may have created.
            remainder = copy.deepcopy(context)
            remainder['process']['revision'] += 1
            resolved = {answer['question_id'] for answer in answers}
            remainder['questions'] = [q for q in remainder['questions'] if q['id'] not in resolved]
            db.execute("""INSERT OR IGNORE INTO wf_question_reviews
                (review_key,process_id,context_hash,evidence_hash,status,attempts,
                 unresolved_count,created_at,updated_at) VALUES (?,?,?,?,'completed',0,?,?,?)""",
                (review_key(remainder, evidence), context['process']['id'], context_hash(remainder),
                 evidence.hash, len(unresolved), stamp, stamp))

    def run_pending(self, store):
        """One bounded pass. Tests may call directly; production uses schedule."""
        from reconcile_questions import pending_context
        if self.stopped.is_set():
            return 0
        store.application_profile = self.runtime.application_profile
        with store.transaction() as db:
            ids = [row[0] for row in db.execute("""SELECT p.id FROM wf_processes p
                JOIN wf_tasks t ON t.id=p.task_id WHERE p.submission_status='preparing'
                AND t.status='waiting_for_user'
                AND EXISTS(SELECT 1 FROM wf_questions q WHERE q.process_id=p.id
                           AND q.superseded=0 AND q.answer IS NULL)
                ORDER BY p.id""")]
            pending_projection = db.execute(
                'SELECT 1 FROM wf_profile_outbox WHERE projected_at IS NULL LIMIT 1').fetchone()
        if not ids:
            return 0
        # The orchestrator normally already flushed the outbox. Avoid taking
        # its file lock again for a scan, especially when another lane starts.
        if pending_projection:
            store.sync_profile_answers(strict=True)
        evidence = self.runtime.evidence_snapshot(application=True)
        saved = 0
        for pid in ids:
            if self.stopped.is_set():
                break
            try:
                context = pending_context(store, pid)
                evidence.assert_current()
            except ReviewError as error:
                if error.status == 409:
                    continue
                raise
            if not context['questions']:
                continue
            claim = self._claim(store, context, evidence)
            if claim is None:
                continue
            key, owner = claim
            lease = RecoveryLease(self, store, context, evidence, key, owner)
            try:
                lease.heartbeat()
                answers, provenance, unresolved = self.runtime.review_questions(
                    context, context['questions'], context['question_bindings'], evidence, lease)
                lease.heartbeat()
                if answers:
                    self.runtime.save_evidence_answers(store, context['process'], answers, provenance, evidence)
                    saved += len(answers)
                self._completed(store, key, owner, context, evidence, answers, unresolved)
            except Exception as error:
                self._failed(store, key, owner, error)
        return saved
