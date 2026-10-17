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

    def lease_task(self, owner, kinds=None, lease_seconds=60):
        text(owner, "Worker owner", 200, required=True)
        _lease_deadline(lease_seconds)  # Validate before acquiring a write lock.
        if kinds is not None and (not isinstance(kinds, (list, tuple, set))
                                  or any(kind not in TASK_KINDS for kind in kinds)):
            raise ReviewError("Task kinds must contain supported kinds.")
        with self.transaction(write=True) as db:
            self._recover_expired_tasks(db)
            rows = db.execute("""SELECT * FROM wf_tasks WHERE status IN ('queued','retry_wait')
                AND available_at<=? AND failure_count<max_attempts ORDER BY created_at,id""", (now(),)).fetchall()
            for row in rows:
                # A health probe is run by its caller while holding the browser.
                # A generic worker must never replay it, including after a stop.
                if _preflight(row):
                    continue
                if kinds is not None and row["kind"] not in kinds:
                    continue
                if row["resource"] is not None and db.execute(
                        "SELECT 1 FROM wf_resource_locks WHERE resource=?", (row["resource"],)).fetchone():
                    continue
                if _submission_started(row):
                    db.execute("UPDATE wf_tasks SET status='waiting_for_user',error=?,updated_at=? WHERE id=?",
                               ("submission_unknown", now(), row["id"]))
                    continue
                token = str(uuid.uuid4())
                deadline = _lease_deadline(lease_seconds)
                db.execute("""UPDATE wf_tasks SET status='running',attempts=attempts+1,
                    lease_owner=?,lease_token=?,lease_until=?,error=NULL,updated_at=? WHERE id=?""",
                    (owner, token, deadline, now(), row["id"]))
                if row["resource"] is not None:
                    db.execute("INSERT INTO wf_resource_locks(resource,task_id,lease_token) VALUES (?,?,?)",
                               (row["resource"], row["id"], token))
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
            if db.execute("SELECT 1 FROM wf_resource_locks WHERE resource='browser'").fetchone():
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
            return self.task(self._task(db, task_id))

    def checkpoint_task(self, task_id, token, checkpoint):
        _json_object(checkpoint, "Checkpoint")
        with self.transaction(write=True) as db:
            row = self._leased_task(db, task_id, token)
            checkpoint = dict(checkpoint)
            previous = json.loads(row["checkpoint"])
            # Dispatch context is backend-owned. A worker progress checkpoint
            # must neither remove it nor replace it with newer unseen facts.
            for field in ("rank_input_lease", "rank_company_fingerprints"):
                if field in previous:
                    if field in checkpoint and checkpoint[field] != previous[field]:
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
