"""SQLite work queue. This module does not run agents or contact employers.

A lease token fences database writes; it cannot stop an old browser worker.
Therefore an expired task that owns a resource keeps that resource quarantined
until an operator confirms the worker stopped. A submission_started checkpoint
also prevents automatic replay after an uncertain submission.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from store import ReviewError, encode, now, text
from readiness import RECOVERABLE_BROWSER_REASONS
from application_pool import browser_resource


TASK_KINDS = {"explore", "rank", "apply", "reconcile"}
TASK_STATES = {"queued", "running", "waiting_for_user", "retry_wait", "succeeded", "failed", "cancelled"}
TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_tasks (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('explore','rank','apply','reconcile')),
 status TEXT NOT NULL CHECK(status IN
   ('queued','running','waiting_for_user','retry_wait','succeeded','failed','cancelled')),
 payload TEXT NOT NULL,
 dedupe_key TEXT NOT NULL,
 resource TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,
 failure_count INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
 available_at TEXT NOT NULL,
 lease_token TEXT,
 lease_owner TEXT,
 lease_until TEXT,
 checkpoint TEXT NOT NULL DEFAULT '{}',
 result TEXT,
 error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS wf_tasks_active_dedupe ON wf_tasks(dedupe_key)
 WHERE status IN ('queued','running','waiting_for_user','retry_wait');
CREATE INDEX IF NOT EXISTS wf_tasks_available ON wf_tasks(status,available_at,created_at);
CREATE TABLE IF NOT EXISTS wf_resource_locks (
 resource TEXT PRIMARY KEY,
 task_id TEXT NOT NULL UNIQUE REFERENCES wf_tasks(id),
 lease_token TEXT NOT NULL,
 quarantined INTEGER NOT NULL DEFAULT 0 CHECK(quarantined IN (0,1))
);
CREATE TABLE IF NOT EXISTS wf_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _json_object(value, label):
    if not isinstance(value, dict):
        raise ReviewError(f"{label} must be a JSON object.")
    try:
        serialized = encode(value)
    except (TypeError, ValueError):
        raise ReviewError(f"{label} must contain valid JSON values.") from None
    if len(serialized.encode("utf-8")) > 1_000_000:
        raise ReviewError(f"{label} is too large.")
    return serialized


def _lease_deadline(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ReviewError("Lease duration must be 1 to 3600 seconds.")
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _submission_started(row):
    return bool(json.loads(row["checkpoint"]).get("submission_started"))


def _preflight(row):
    return row["kind"] == "reconcile" and json.loads(row["payload"]).get("purpose") == "preflight"


class TaskMixin:
    @staticmethod
    def task(row):
        value = dict(row)
        for key in ("payload", "checkpoint", "result"):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        value["resource_quarantined"] = bool(value.get("resource_quarantined", False))
        return value

    @staticmethod
    def _task(db, task_id):
        row = db.execute("""SELECT t.*, COALESCE(l.quarantined,0) AS resource_quarantined
            FROM wf_tasks t LEFT JOIN wf_resource_locks l ON l.task_id=t.id WHERE t.id=?""", (task_id,)).fetchone()
        if row is None:
            raise ReviewError("Task does not exist.", 404)
        return row

    def _tasks(self, db):
        return [self.task(row) for row in db.execute("""SELECT t.*,
            COALESCE(l.quarantined,0) AS resource_quarantined FROM wf_tasks t
            LEFT JOIN wf_resource_locks l ON l.task_id=t.id ORDER BY t.created_at,t.id""")]

    def tasks(self):
        with self.transaction() as db:
            return self._tasks(db)

    def _enqueue_task(self, db, kind, payload, dedupe_key, *, resource=None):
        if kind not in TASK_KINDS:
            raise ReviewError("Unsupported task kind.")
        payload_json = _json_object(payload, "Task payload")
        text(dedupe_key, "Task deduplication key", 1000, required=True)
        if resource is not None:
            text(resource, "Task resource", 200, required=True)
        existing = db.execute("""SELECT id FROM wf_tasks WHERE dedupe_key=? AND
            status IN ('queued','running','waiting_for_user','retry_wait')""", (dedupe_key,)).fetchone()
        if existing:
            return existing["id"]
        stamp, task_id = now(), str(uuid.uuid4())
        db.execute("""INSERT INTO wf_tasks
            (id,kind,status,payload,dedupe_key,resource,available_at,created_at,updated_at)
            VALUES (?,?,'queued',?,?,?,?,?,?)""",
            (task_id, kind, payload_json, dedupe_key, resource, stamp, stamp, stamp))
        return task_id

    def _leased_task(self, db, task_id, token):
        text(token, "Lease token", 200, required=True)
        row = self._task(db, task_id)
        if (row["status"] != "running" or row["lease_token"] != token
                or row["lease_until"] is None or row["lease_until"] <= now()
                or row["resource_quarantined"]):
            raise ReviewError("Task lease is stale or belongs to another worker.", 409)
        if row["resource"] is not None:
            lock = db.execute("SELECT * FROM wf_resource_locks WHERE resource=?", (row["resource"],)).fetchone()
            if lock is None or lock["task_id"] != task_id or lock["lease_token"] != token:
                raise ReviewError("Task resource lease is no longer owned by this worker.", 409)
        return row

    def lease_task(self, owner, kinds=None, lease_seconds=60, *, application_worker_id=None):
        text(owner, "Worker owner", 200, required=True)
        _lease_deadline(lease_seconds)  # Validate before acquiring a write lock.
        if kinds is not None and (not isinstance(kinds, (list, tuple, set))
                                  or any(kind not in TASK_KINDS for kind in kinds)):
            raise ReviewError("Task kinds must contain supported kinds.")
        with self.transaction(write=True) as db:
            self._recover_expired_tasks(db)
            resumed = db.execute("SELECT value FROM wf_settings WHERE key='browser_resume_task'").fetchone()
            rows = db.execute("""SELECT * FROM wf_tasks WHERE status IN ('queued','retry_wait')
                AND available_at<=? AND failure_count<max_attempts
                ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,created_at,id""",
                (now(), resumed['value'] if resumed else None)).fetchall()
            if application_worker_id is not None:
                if getattr(self,'question_pool',None) is not None and db.execute(
                        "SELECT 1 FROM wf_qpool_lock WHERE token IS NOT NULL AND expires_at>? "
                        "AND owner_kind IN ('answer_handler','recovery')",(now(),)).fetchone():
                    return None
                candidate = self._prepare_slot_candidate(db, application_worker_id, rows)
                rows = [candidate] if candidate is not None else []
            else:
                assigned = ({r[0] for r in db.execute('SELECT task_id FROM wf_apply_slots WHERE task_id IS NOT NULL')}
                            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wf_apply_slots'").fetchone() else set())
                rows = [row for row in rows if row['id'] not in assigned]
            resumed_id = next((row['id'] for row in rows if resumed and row['id'] == resumed['value']
                and row['resource'] == 'browser' and row['kind'] in ('apply', 'explore')
                and not _submission_started(row)), None)
            for row in rows:
                # A health probe is run by its caller while holding the browser.
                # A generic worker must never replay it, including after a stop.
                if _preflight(row):
                    continue
                if kinds is not None and row["kind"] not in kinds:
                    continue
                # A resumed application retains priority over new discovery.
                if row['resource'] == 'browser' and (resumed_id and row['id'] != resumed_id):
                    continue
                if row["resource"] is not None and db.execute(
                        "SELECT 1 FROM wf_resource_locks WHERE resource=?", (row["resource"],)).fetchone():
                    continue
                if _submission_started(row):
                    db.execute("UPDATE wf_tasks SET status='waiting_for_user',error=?,updated_at=? WHERE id=?",
                               ("submission_unknown", now(), row["id"]))
                    continue
                if (not getattr(self,'browser_access_serialized',False)
                        and application_worker_id is not None and db.execute(
                        "SELECT 1 FROM wf_resource_locks WHERE resource='browser'").fetchone()):
                    continue
                # A legacy/global browser caller cannot overlap an isolated slot.
                if (not getattr(self,'browser_access_serialized',False)
                        and row['resource'] == 'browser' and db.execute(
                        "SELECT 1 FROM wf_resource_locks WHERE resource LIKE 'browser:apply-%'").fetchone()):
                    continue
                token = str(uuid.uuid4())
                deadline = _lease_deadline(lease_seconds)
                db.execute("""UPDATE wf_tasks SET status='running',attempts=attempts+1,
                    lease_owner=?,lease_token=?,lease_until=?,error=NULL,updated_at=? WHERE id=?""",
                    (owner, token, deadline, now(), row["id"]))
                if row["resource"] is not None:
                    db.execute("INSERT INTO wf_resource_locks(resource,task_id,lease_token) VALUES (?,?,?)",
                               (row["resource"], row["id"], token))
                if row['resource'] == 'browser':
                    db.execute("DELETE FROM wf_settings WHERE key='browser_resume_task' AND value=?", (row['id'],))
                return self.task(self._task(db, row["id"]))
        return None

    def lease_preflight(self, owner, probe_id, lease_seconds=60):
        """Reserve the browser and create a running probe in one transaction.

        Return None when the browser is busy or this probe ID was used before.
        Busy calls leave no queued task. An expired browser lease remains locked
        until the prior worker or probe is known to have stopped.
        """
        text(owner, "Worker owner", 200, required=True)
        text(probe_id, "Probe ID", 200, required=True)
        _lease_deadline(lease_seconds)
        dedupe_key = "preflight:" + probe_id
        with self.transaction(write=True) as db:
            self._recover_expired_tasks(db)
            if db.execute("SELECT 1 FROM wf_resource_locks WHERE resource='browser'" +
                    ('' if getattr(self,'browser_access_serialized',False) else " OR resource LIKE 'browser:apply-%'")).fetchone():
                return None
            # Probe IDs identify one execution, not an automatically replayable
            # command. Retain that distinction after a terminal result as well.
            if db.execute("SELECT 1 FROM wf_tasks WHERE dedupe_key=?", (dedupe_key,)).fetchone():
                return None
            task_id = self._enqueue_task(db, "reconcile", {
                "purpose": "preflight", "probe_id": probe_id,
            }, dedupe_key, resource="browser")
            token = str(uuid.uuid4())
            db.execute("""UPDATE wf_tasks SET status='running',attempts=1,max_attempts=1,
                lease_owner=?,lease_token=?,lease_until=?,updated_at=? WHERE id=?""",
                (owner, token, _lease_deadline(lease_seconds), now(), task_id))
            db.execute("INSERT INTO wf_resource_locks(resource,task_id,lease_token) VALUES ('browser',?,?)",
                       (task_id, token))
            return self.task(self._task(db, task_id))

    def _preflight_lock(self, db, task_id, token):
        text(token, "Lease token", 200, required=True)
        row = self._task(db, task_id)
        if not _preflight(row) or row["resource"] != "browser":
            raise ReviewError("Task is not a browser preflight probe.", 409)
        lock = db.execute("SELECT * FROM wf_resource_locks WHERE resource='browser'").fetchone()
        # A completed probe or an operator-released probe cannot affect a later
        # owner, even when its synchronous call finally returns after a timeout.
        if lock is None or lock["task_id"] != task_id:
            return row, None
        if lock["lease_token"] != token:
            raise ReviewError("Preflight lease belongs to another probe.", 409)
        return row, lock

    def timeout_preflight(self, task_id, token):
        """Park a timed-out probe without making the browser available again.

        Repeating the call with the original lock token is safe. Calls after
        release return the old task unchanged and cannot quarantine a new owner.
        """
        with self.transaction(write=True) as db:
            row, lock = self._preflight_lock(db, task_id, token)
            if lock is None:
                return self.task(row)
            db.execute("""UPDATE wf_resource_locks SET quarantined=1
                WHERE resource='browser' AND task_id=? AND lease_token=?""", (task_id, token))
            db.execute("""UPDATE wf_tasks SET status='waiting_for_user',error='preflight_timeout',
                failure_count=MAX(failure_count,1),lease_token=NULL,lease_owner=NULL,
                lease_until=NULL,updated_at=? WHERE id=?""", (now(), task_id))
            return self.task(self._task(db, task_id))

    def stop_preflight(self, task_id, token, *, success, error=None):
        """Release a probe ONLY after its synchronous adapter call has returned.

        The caller must have positive evidence that the call stopped. Unlike a
        normal task finish, this accepts the original lock token after expiry or
        quarantine. A task whose lock was already released is a safe no-op.
        """
        if type(success) is not bool:
            raise ReviewError("Preflight success must be true or false.")
        if error is not None:
            text(error, "Task error", 8000, required=True)
        with self.transaction(write=True) as db:
            row, lock = self._preflight_lock(db, task_id, token)
            if lock is None:
                return self.task(row)
            db.execute("""DELETE FROM wf_resource_locks
                WHERE resource='browser' AND task_id=? AND lease_token=?""", (task_id, token))
            db.execute("""UPDATE wf_tasks SET status=?,error=?,
                failure_count=MAX(failure_count,?),lease_token=NULL,lease_owner=NULL,
                lease_until=NULL,updated_at=? WHERE id=?""",
                ("succeeded" if success else "failed", error, 0 if success else 1, now(), task_id))
            return self.task(self._task(db, task_id))

    def heartbeat_task(self, task_id, token, lease_seconds=60):
        _lease_deadline(lease_seconds)
        with self.transaction(write=True) as db:
            self._leased_task(db, task_id, token)
            deadline = _lease_deadline(lease_seconds)
            db.execute("UPDATE wf_tasks SET lease_until=?,updated_at=? WHERE id=?", (deadline, now(), task_id))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='wf_apply_slots'").fetchone():
                changed=db.execute('UPDATE wf_apply_slots SET updated_at=? WHERE task_id=?',(now(),task_id))
                if changed.rowcount:
                    self._pool_bump(db)
            return self.task(self._task(db, task_id))

    def checkpoint_task(self, task_id, token, checkpoint, *, merge=False):
        if type(merge) is not bool:
            raise ReviewError("Checkpoint merge must be a boolean.")
        _json_object(checkpoint, "Checkpoint")
        with self.transaction(write=True) as db:
            row = self._leased_task(db, task_id, token)
            checkpoint = dict(checkpoint)
            previous = json.loads(row["checkpoint"])
            if merge:
                checkpoint = {**previous, **checkpoint}
            # The host owns the provider retry budget, including its initial zero.
            if ('worker_capacity_failures' in checkpoint and
                    (type(checkpoint['worker_capacity_failures']) is not int or
                     checkpoint['worker_capacity_failures'] != previous.get('worker_capacity_failures', 0))):
                raise ReviewError('A worker cannot change its capacity retry budget.', 409)
            if 'worker_capacity_failures' in previous:
                checkpoint['worker_capacity_failures'] = previous['worker_capacity_failures']
            if ('readiness_recovery_count' in checkpoint and
                    (type(checkpoint['readiness_recovery_count']) is not int or
                     checkpoint['readiness_recovery_count'] != previous.get('readiness_recovery_count', 0))):
                raise ReviewError('A worker cannot change its readiness recovery budget.', 409)
            if 'readiness_recovery_count' in previous:
                checkpoint['readiness_recovery_count'] = previous['readiness_recovery_count']
            # Dispatch context is backend-owned. A worker progress checkpoint
            # must neither remove it nor replace it with newer unseen facts.
            for field in ('application_worker_id','pool_session_reset','pool_transport_proof','application_tab_binding'):
                if field in checkpoint and field not in previous:
                    raise ReviewError('A worker cannot introduce host-owned application context.',409)
            for field in ("rank_input_lease", "rank_company_fingerprints", "application_worker_id", "pool_session_reset", "pool_transport_proof", "application_tab_binding"):
                if field in previous:
                    if field in checkpoint and encode(checkpoint[field]) != encode(previous[field]):
                        raise ReviewError("A worker cannot change the dispatched rank context.", 409)
                    checkpoint[field] = previous[field]
            if _submission_started(row) and not checkpoint.get("submission_started"):
                raise ReviewError("A submission checkpoint cannot be cleared by a worker.", 409)
            serialized = _json_object(checkpoint, "Checkpoint")
            db.execute("UPDATE wf_tasks SET checkpoint=?,updated_at=? WHERE id=?", (serialized, now(), task_id))
            return self.task(self._task(db, task_id))

    def _release_task(self, db, task_id, status, *, result=None, error=None, available_at=None):
        if status not in TASK_STATES or status == "running":
            raise ReviewError("Unsupported released task status.")
        row = self._task(db, task_id)
        if row["resource_quarantined"]:
            raise ReviewError("Confirm the old worker stopped before releasing its resource.", 409)
        result_json = None if result is None else _json_object(result, "Task result")
        if error is not None:
            text(error, "Task error", 8000, required=True)
        db.execute("DELETE FROM wf_resource_locks WHERE task_id=?", (task_id,))
        db.execute("""UPDATE wf_tasks SET status=?,result=?,error=?,available_at=?,
            lease_token=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
            (status, result_json, error, available_at or now(), now(), task_id))
        return self.task(self._task(db, task_id))

    def finish_task(self, task_id, token, result):
        with self.transaction(write=True) as db:
            row = self._leased_task(db, task_id, token)
            if row["kind"] == "apply":
                raise ReviewError("Application tasks must finish through the application receipt flow.", 409)
            return self._release_task(db, task_id, "succeeded", result=result)

    def fail_task(self, task_id, token, error, retryable=True):
        text(error, "Task error", 8000, required=True)
        if type(retryable) is not bool:
            raise ReviewError("Retryable must be true or false.")
        with self.transaction(write=True) as db:
            row = self._leased_task(db, task_id, token)
            if _submission_started(row):
                return self._release_task(db, task_id, "waiting_for_user", error="submission_unknown: " + error[:7900])
            failure_count = row["failure_count"] + 1
            db.execute("UPDATE wf_tasks SET failure_count=? WHERE id=?", (failure_count, task_id))
            retry = retryable and failure_count < row["max_attempts"]
            delay = min(300, 5 * (2 ** min(6, failure_count - 1)))
            available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="microseconds")
            return self._release_task(db, task_id, "retry_wait" if retry else "failed", error=error,
                                      available_at=available_at if retry else None)

    def schedule_browser_recovery(self, task_id, token, reason):
        """Retry only a stopped browser call with a host-recognized reason.

        Keep the original task, approval, answers, and checkpoints. The existing
        failure budget and durable availability time also survive service restart.
        """
        if reason not in RECOVERABLE_BROWSER_REASONS:
            raise ReviewError("This browser blocker does not allow automatic recovery.")
        with self.transaction(True) as db:
            row = self._leased_task(db, task_id, token)
            if not browser_resource(row["resource"]) or row["kind"] not in {"apply", "explore"}:
                raise ReviewError("Only browser application or exploration work can recover.", 409)
            if _submission_started(row):
                return self._release_task(db, task_id, "waiting_for_user", error="submission_unknown")
            failures = row["failure_count"] + 1
            retry = failures < row["max_attempts"]
            delay = min(300, 5 * (2 ** min(6, failures - 1)))
            available = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="microseconds")
            db.execute("UPDATE wf_tasks SET failure_count=? WHERE id=?", (failures, task_id))
            result = self._release_task(db, task_id, "retry_wait" if retry else "waiting_for_user", error=reason,
                                        available_at=available if retry else None)
            if row["kind"] == "apply":
                pid = json.loads(row["payload"])["process_id"]
                if not retry:
                    db.execute("UPDATE queue SET status='needs_input',updated_at=? WHERE id=?", (now(), pid))
                    db.execute('UPDATE wf_processes SET block_reason=?,revision=revision+1,updated_at=? WHERE id=?',
                               (reason, now(), pid))
                self._process_event(db, pid, "browser_recovery_scheduled" if retry else "browser_recovery_exhausted",
                                    {"reason": reason, "failure_count": failures,
                                     "retry_at": available if retry else None}, "orchestrator")
            return result

    def schedule_worker_capacity_recovery(self, task_id, token):
        """Bound provider retries separately from application execution failures."""
        with self.transaction(True) as db:
            row = self._leased_task(db, task_id, token)
            if _submission_started(row):
                return self._release_task(db, task_id, 'waiting_for_user', error='submission_unknown')
            checkpoint = json.loads(row['checkpoint'])
            previous = checkpoint.get('worker_capacity_failures', 0)
            if type(previous) is not int or not 0 <= previous <= 4:
                raise ReviewError('Invalid worker capacity recovery state.', 409)
            count = min(previous + 1, 4)
            checkpoint['worker_capacity_failures'] = count
            retry = count <= 3
            available = (datetime.now(timezone.utc) + timedelta(seconds=30 * 2 ** (count - 1))).isoformat(timespec='microseconds')
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?', (encode(checkpoint), task_id))
            result = self._release_task(db, task_id, 'retry_wait' if retry else 'failed',
                error='model_at_capacity', available_at=available if retry else None)
            if row['kind'] == 'apply':
                pid = json.loads(row['payload'])['process_id']
                self._process_event(db, pid, 'worker_capacity_retry' if retry else 'worker_capacity_exhausted',
                    {'reason': 'model_at_capacity', 'failure_count': count,
                     'retry_at': available if retry else None}, 'orchestrator')
            return result

    def resume_recoverable_task(self, task_id, expected_updated_at, *, reason, evidence_reference,
                                process_revision=None):
        """Operator recovery after inspecting an exact parked task's prerequisite.

        This is deliberately not an HTTP command or a text-based classifier.
        Evidence is an audit reference, not instructions or new authorization.
        Every resumed browser task still runs the normal readiness check.
        """
        if reason not in RECOVERABLE_BROWSER_REASONS | {"saved_profile_reconciled"}:
            raise ReviewError("Unsupported recovery reason.")
        text(expected_updated_at, "Expected task update", 100, required=True)
        text(evidence_reference, "Recovery evidence reference", 2000, required=True)
        with self.transaction(True) as db:
            row = self._task(db, task_id)
            if row["updated_at"] != expected_updated_at:
                raise ReviewError("Task changed before recovery.", 409, reason="stale_revision")
            if not browser_resource(row["resource"]) or row["kind"] not in {"apply", "explore"}:
                raise ReviewError("This task is not eligible for browser recovery.", 409)
            allowed = ({"posting_changed"} if reason == "saved_profile_reconciled" else
                       RECOVERABLE_BROWSER_REASONS | {"application_action_required"})
            if row["error"] not in allowed:
                raise ReviewError("This task has a different unresolved blocker.", 409)
            process = None
            if row["kind"] == "apply":
                pid = json.loads(row["payload"])["process_id"]
                process = self._process(db, pid)
                if type(process_revision) is not int or process["revision"] != process_revision:
                    raise ReviewError("Application changed before recovery.", 409, reason="stale_revision")
                if process["submission_status"] not in {"not_started", "preparing"}:
                    raise ReviewError("Reconcile the submission outcome before recovery.", 409)
                self._check_application(db, process)
                if db.execute("""SELECT 1 FROM wf_questions WHERE process_id=? AND superseded=0
                    AND required=1 AND answer IS NULL""", (pid,)).fetchone():
                    raise ReviewError("Required application answers are still missing.", 409)
                if db.execute("SELECT 1 FROM wf_submission_attempts WHERE process_id=?", (pid,)).fetchone():
                    raise ReviewError("A submission attempt must be reconciled before recovery.", 409)
            elif reason == "saved_profile_reconciled":
                raise ReviewError("Profile recovery requires an application task.", 409)
            result = self._resume_task(db, task_id)
            if process is not None:
                stamp = now()
                db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?", (stamp, process["id"]))
                db.execute("""UPDATE wf_processes SET block_reason=NULL,revision=revision+1,
                    updated_at=? WHERE id=?""", (stamp, process["id"]))
                self._process_event(db, process["id"], "recovery_requested",
                                    {"reason": reason, "evidence_reference": evidence_reference,
                                     "previous_task_updated_at": expected_updated_at,
                                     "previous_process_revision": process_revision}, "operator")
            return result

    def quarantine_task(self, task_id, token, error="browser_work_uncertain"):
        """Fence uncertain browser work without making its resource available.

        Killing an agent process does not prove that a browser command stopped.
        Only acknowledge_worker_stopped may clear this resource quarantine.
        """
        text(error, "Task error", 8000, required=True)
        with self.transaction(write=True) as db:
            row = self._leased_task(db, task_id, token)
            if row["resource"] is None:
                raise ReviewError("Only a task with an external resource can be quarantined.", 409)
            db.execute("UPDATE wf_resource_locks SET quarantined=1 WHERE task_id=? AND lease_token=?",
                       (task_id, token))
            reason = "submission_unknown: " + error[:7900] if _submission_started(row) else error
            db.execute("""UPDATE wf_tasks SET status='waiting_for_user',error=?,failure_count=failure_count+1,
                lease_token=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                       (reason, now(), task_id))
            return self.task(self._task(db, task_id))

    def _recover_expired_tasks(self, db):
        rows = db.execute("SELECT * FROM wf_tasks WHERE status='running' AND lease_until<=?", (now(),)).fetchall()
        for row in rows:
            uncertain = _submission_started(row)
            failure_count = row["failure_count"] + 1
            db.execute("UPDATE wf_tasks SET failure_count=? WHERE id=?", (failure_count, row["id"]))
            if row["resource"] is not None:
                db.execute("""INSERT INTO wf_resource_locks(resource,task_id,lease_token,quarantined)
                    VALUES (?,?,?,1) ON CONFLICT(resource) DO UPDATE SET quarantined=1""",
                    (row["resource"], row["id"], row["lease_token"]))
                db.execute("""UPDATE wf_tasks SET status='waiting_for_user',error=?,
                    lease_token=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                    ("submission_unknown" if uncertain else "worker_lease_lost", now(), row["id"]))
            elif uncertain:
                self._release_task(db, row["id"], "waiting_for_user", error="submission_unknown")
            else:
                self._release_task(db, row["id"], "retry_wait" if failure_count < row["max_attempts"] else "failed",
                                   error="worker_lease_lost", available_at=_lease_deadline(5))
        return len(rows)

    def recover_expired_tasks(self):
        with self.transaction(write=True) as db:
            return self._recover_expired_tasks(db)

    def acknowledge_worker_stopped(self, task_id):
        """Clear quarantine only. This does not resume or replay the task."""
        with self.transaction(write=True) as db:
            row = self._task(db, task_id)
            if row["status"] != "waiting_for_user" or not row["resource_quarantined"]:
                raise ReviewError("Task does not have a quarantined resource.", 409)
            db.execute("DELETE FROM wf_resource_locks WHERE task_id=?", (task_id,))
            db.execute("UPDATE wf_tasks SET updated_at=? WHERE id=?", (now(), task_id))
            return self.task(self._task(db, task_id))

    def _resume_task(self, db, task_id):
        row = self._task(db, task_id)
        if row["error"] == "ranked_output_pending":
            raise ReviewError("Accepted ranking is waiting for its output file; retry export instead.", 409)
        if row["status"] != "waiting_for_user" or row["resource_quarantined"]:
            raise ReviewError("Task cannot resume until its worker has stopped.", 409)
        if _submission_started(row):
            raise ReviewError("Reconcile the submission outcome before any new attempt.", 409)
        if row["failure_count"] >= row["max_attempts"]:
            raise ReviewError("Task has reached its attempt limit.", 409)
        return self._release_task(db, task_id, "queued")

    def _cancel_task(self, db, task_id):
        row = self._task(db, task_id)
        if row["status"] not in ("queued", "retry_wait"):
            raise ReviewError("Only work that has not started can be cancelled.", 409)
        return self._release_task(db, task_id, "cancelled")
