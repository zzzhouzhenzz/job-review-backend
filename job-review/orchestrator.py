"""Durable scheduling and task-scoped dispatch through an injected runtime."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Protocol

from store import ReviewError, now
from application_pool import slot_for_task, browser_resource
from applicant_evidence import EvidenceChanged
from codex_transport import CodexWorkerStopped, SessionOnlyHandoffUnverified
from readiness import BrowserNotReady, BrowserRecoverable, ReadinessMonitor, WorkerCapacityUnavailable


LOGGER = logging.getLogger(__name__)
_CODE_DIRECTORY = Path(__file__).resolve().parent
_DIAGNOSTIC_REASONS = frozenset({
    "profile_busy", "profile_pending", "profile_changed", "profile_sync_failed",
    "profile_invalid", "profile_too_large", "profile_block_invalid",
    "profile_status_unavailable", "applicant_evidence_changed", "posting_review_invalid",
})


def _log_failure(error, *, level=logging.ERROR):
    """Keep source locations and error categories, never messages or locals."""
    reason = getattr(error, "reason", None)
    details = {"type": type(error).__name__,
               "reason": reason if isinstance(reason, str) and reason in _DIAGNOSTIC_REASONS else "unclassified"}
    locations, trace = [], error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        source = Path(code.co_filename)
        if source.resolve().parent == _CODE_DIRECTORY:
            locations.append({"file": source.name, "function": code.co_name, "line": trace.tb_lineno})
        trace = trace.tb_next
    details["traceback"] = locations[-8:]
    cause = error.__cause__ or error.__context__
    if cause is not None:
        details["cause_type"] = type(cause).__name__
        number = getattr(cause, "errno", None)
        if type(number) is int and 0 <= number <= 4095:
            details["cause_errno"] = number
    LOGGER.log(level, "Worker dispatch failed: %s", json.dumps(details, sort_keys=True))


class AgentRuntime(Protocol):
    """The runtime owns dedicated sessions, source access, and browser operations.

    Apply must use the fenced ApplicationMixin lifecycle around every side effect.
    It may return only after parking for questions or confirming a receipt. It must
    never use generic task completion to claim that an application was submitted.
    Rank returns complete plain-text card content and ordered selections as
    defined in ranking.py. The iOS client owns layout and rendering.
    """
    enabled: bool

    def prepare_browser(self, task: dict, lease: "TaskLease") -> dict:
        """Check/reuse a signed-in LinkedIn tab under the task's browser lease.

        Return only when check operations stopped. Raise BrowserWorkUncertain
        if an action may still be active. No check runs at server startup.
        """
        ...

    def explore(self, context: dict, lease: "TaskLease") -> dict: ...
    def rank(self, context: dict, lease: "TaskLease") -> dict: ...
    def apply(self, store, task: dict) -> None: ...


class UnconfiguredRuntime:
    enabled = False

    supported_kinds = ()

    def prepare_browser(self, task, lease):
        raise NotImplementedError("The Chrome runtime is not configured.")

    def explore(self, context, lease):
        raise NotImplementedError("Explorer agent is not configured.")

    def rank(self, context, lease):
        raise NotImplementedError("Ranker agent is not configured.")

    def apply(self, store, task):
        raise NotImplementedError("Application agent is not configured.")


class TaskLease:
    """Runtime must heartbeat during long calls and stop if renewal fails."""
    def __init__(self, store, task):
        self.store, self.task_id, self.token = store, task["id"], task["lease_token"]
        self.application_worker_id = slot_for_task(task)
        checkpoint=task.get('checkpoint',{})
        self.pool_session_reset=checkpoint.get('pool_session_reset')
        self._pool_transport_proof=checkpoint.get('pool_transport_proof')

    @property
    def session_only_handoff_verified(self):
        from application_pool import current_transport_proof
        current=current_transport_proof(self.store)
        return bool(current and current == self._pool_transport_proof)

    def verify_pool_transport(self):
        if self.application_worker_id and not self.session_only_handoff_verified:
            raise SessionOnlyHandoffUnverified('Application pool transport proof is absent or stale.')

    def admit_application_tab(self, role, native_thread_id):
        binding=self.store.application_tab_binding(self.task_id,self.token,self.application_worker_id,
            native_thread_id=native_thread_id,
            expected_binding_id=getattr(self,'_application_tab_binding_id',None),
            required=role in ('application_tab_check','application_prepare','application_submit'))
        if binding is not None and (native_thread_id is None or role=='application_tab_open'):
            raise ReviewError('The existing application tab requires its exact native session.',409)
        if binding:
            self._application_tab_binding_id=binding['id']
        return binding

    def heartbeat(self):
        guard=getattr(self,"pool_guard",None)
        if guard is not None:
            guard()
        return self.store.heartbeat_task(self.task_id, self.token)

    def browser_gate_blockers(self):
        # Legacy quarantine is proof of an unconfirmed owner, not a mere
        # application outcome. Only an audited stopped-owner action clears it.
        with self.store.transaction() as db:
            self.store._leased_task(db, self.task_id, self.token)
            return [{'task_id': row['task_id'], 'resource': row['resource']}
                    for row in db.execute('SELECT task_id,resource FROM wf_resource_locks '
                                          'WHERE quarantined=1 ORDER BY resource')
                    if browser_resource(row['resource'])]

    def stage(self, name, *, state='running'):
        if self.application_worker_id:
            self.store.application_stage(self.application_worker_id, name, state=state,
                                         task_id=self.task_id,lease_token=self.token)

    def checkpoint(self, value):
        return self.store.checkpoint_task(self.task_id, self.token, value, merge=True)


class Orchestrator:
    def __init__(self, store, runtime=None, owner="local-worker", *, readiness_options=None,
                 schedule_answer_reviews=True):
        self.store = store
        self.runtime = runtime if runtime is not None else UnconfiguredRuntime()
        profile = getattr(self.runtime, 'application_profile', None)
        if profile is not None:
            self.store.application_profile = profile
            self.store.backfill_profile_answers()
            self.store.sync_profile_answers()
        self.owner = owner
        gate = getattr(getattr(self.runtime, 'transport', None), 'chrome_access_gate', None)
        if gate is not None:
            self.store.browser_access_snapshot = gate.snapshot
            self.store.browser_access_serialized = True
        if getattr(self.runtime, 'question_pool_enabled', False):
            from pool_application import initialize
            initialize(self.runtime, self.store)
        self.schedule_answer_reviews = schedule_answer_reviews
        changed = getattr(self.runtime, 'answers_changed', None)
        self.store.on_answers_changed = (
            (lambda: changed(self.store)) if schedule_answer_reviews and callable(changed) else None)
        self.readiness = ReadinessMonitor(store, self.runtime, **(readiness_options or {}))

    def tick(self):
        """Recover local state and schedule review; never wait for agent work."""
        from pool_application import recover
        recover(self.store)
        synchronized = self.store.sync_profile_answers()
        reuse = getattr(self.runtime, 'reuse_pending_answers', None)
        if self.schedule_answer_reviews and synchronized.get('status') == 'current' and callable(reuse):
            reuse(self.store)
        recovered = self.store.recover_expired_tasks()
        self.store.reconcile_processes()
        outputs = self.store.repair_ranked_outputs()
        refill = self.store.schedule_refill()
        return {"worker_enabled": bool(self.runtime.enabled), "recovered_tasks": recovered,
                "ranked_outputs": outputs, "refill_task_id": refill}

    def run_once(self, kinds=None, *, application_worker_id=None):
        try:
            return self._run_once(kinds, application_worker_id=application_worker_id)
        except Exception as error:
            _log_failure(error)
            raise
        finally:
            if application_worker_id:
                self.store.refresh_application_slot(application_worker_id)

    def _requeue_profile_busy(self, task, token):
        with self.store.transaction(True) as db:
            current = self.store._leased_task(db, task["id"], token)
            if json.loads(current["checkpoint"]).get("submission_started"):
                return False
            self.store._release_task(db, task["id"], "queued", error="profile_busy")
            db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?",
                       (now(), task["payload"]["process_id"]))
        return True


    def _run_once(self, kinds=None, *, application_worker_id=None):
        self.tick()
        if not self.runtime.enabled:
            return {"worker_status": "not_configured", "task_id": None}
        # A committed phone answer must reach the profile before a worker reads it.
        try:
            self.store.sync_profile_answers(strict=True)
        except ReviewError as error:
            if error.reason != "profile_busy":
                raise
            # A concurrent answer review may briefly hold the profile lock.
            # No task has been leased or browser operation started yet.
            LOGGER.debug('Worker waiting for local profile synchronization (profile_busy).')
            return {"worker_status": "profile_busy", "task_id": None}
        requested = {"apply", "rank", "explore"} if kinds is None else set(kinds)
        if not requested.issubset({"apply", "rank", "explore"}):
            raise ValueError("Worker task kinds must be apply, rank, or explore.")
        supported = set(getattr(self.runtime, "supported_kinds", {"apply", "rank", "explore"}))
        allowed = requested & supported
        if not allowed:
            return {"worker_status": "not_configured", "task_id": None}
        consent_missing = bool(allowed & {"apply", "explore"}) and not self.store.project_consent()["granted"]
        if consent_missing:
            # Leave browser tasks untouched and unleased. The first accepted
            # grant lets normal polling resume them without spending retries.
            allowed -= {"apply", "explore"}
            if not allowed:
                return {"worker_status": "project_consent_required", "task_id": None}
        self.readiness.poll()
        if not self.readiness.storage_ready:
            return {"worker_status": self.readiness.snapshot()["status"], "task_id": None}
        task = self.store.lease_task(self.owner, kinds=sorted(allowed), application_worker_id=application_worker_id)
        if task is None:
            return {"worker_status": "project_consent_required" if consent_missing else "idle", "task_id": None}
        token = task["lease_token"]
        application_started = False
        try:
            lease = TaskLease(self.store, task)
            lease.stage("loading_answers")
            if task["kind"] in {"apply", "explore"}:
                task["browser"] = self.readiness.prepare_browser(task, lease)
            if task["kind"] == "apply":
                self.store.start_application(task["payload"]["process_id"], token)
                application_started = True
                lease.stage("applying")
                self.runtime.apply(self.store, task)
                with self.store.transaction() as db:
                    current = self.store._task(db, task["id"])
                    if current["status"] == "running":
                        raise RuntimeError("Application runtime returned without a receipt or a durable wait state.")
            else:
                context = self.store.discovery_context(task["id"], token)
                if task["kind"] == "explore":
                    context["browser"] = task["browser"]
                    self.store.accept_exploration(task["id"], token, self.runtime.explore(context, lease))
                else:
                    self.store.accept_ranking(task["id"], token, self.runtime.rank(context, lease))
        except SessionOnlyHandoffUnverified:
            with self.store.transaction(True) as db:
                current=self.store._leased_task(db,task['id'],token)
                self.store._release_task(db,task['id'],'waiting_for_user',error='pool_transport_verification_required')
                if task['kind']=='apply':
                    pid=task['payload']['process_id']
                    db.execute("UPDATE queue SET status='needs_input',updated_at=? WHERE id=?",(now(),pid))
                    db.execute("UPDATE wf_processes SET block_reason='pool_transport_verification_required',revision=revision+1,updated_at=? WHERE id=?",(now(),pid))
            return {'worker_status':'blocked','task_id':task['id']}
        except CodexWorkerStopped:
            # The transport returns this only after local work has stopped, or
            # before spawning any call. A browser-started cancellation never
            # reaches this branch. Lease fencing still guards every transition.
            with self.store.transaction(True) as db:
                current = self.store._leased_task(db, task["id"], token)
                uncertain = bool(json.loads(current["checkpoint"]).get("submission_started"))
                self.store._release_task(db, task["id"], "waiting_for_user" if uncertain else "queued",
                                         error="submission_unknown" if uncertain else "worker_stopped")
                if task["kind"] == "apply":
                    db.execute("UPDATE queue SET status=?,updated_at=? WHERE id=?",
                               ("needs_input" if uncertain else "queued", now(), task["payload"]["process_id"]))
            self.store.reconcile_processes()
            return {"worker_status": "submission_unknown" if uncertain else "stopped", "task_id": task["id"]}
        except EvidenceChanged:
            # Another accepted answer can update the profile during form prep.
            # No submission has started; reread it without spending failure budget.
            with self.store.transaction(True) as db:
                current = self.store._task(db, task['id'])
                if json.loads(current['checkpoint']).get('submission_started'):
                    raise ReviewError('Cannot repeat an uncertain submission.', 409)
                requeued = False
                if current['status'] == 'running':
                    self.store._leased_task(db, task['id'], token)
                    self.store._release_task(db, task['id'], 'queued', error='applicant_evidence_changed')
                    requeued = True
                elif current['status'] == 'waiting_for_user' and current['error'] == 'questions_pending':
                    # ask_questions released our lease before the evidence save.
                    # Reprepare this round; preserve any answers already accepted.
                    self.store._resume_task(db, task['id'])
                    requeued = True
                if requeued and task['kind'] == 'apply':
                    pid = task['payload']['process_id']
                    db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?", (now(), pid))
            return {'worker_status': 'evidence_changed', 'task_id': task['id']}
        except WorkerCapacityUnavailable:
            try:
                result = self.store.schedule_worker_capacity_recovery(task['id'], token)
            except ReviewError:
                self.store.recover_expired_tasks()
                raise
            self.readiness.worker_capacity_failed(
                retry_at=result['available_at'] if result['status'] == 'retry_wait' else None)
            self.store.reconcile_processes()
            return {'worker_status': result['status'], 'task_id': task['id']}
        except BrowserRecoverable as error:
            # Only typed, stopped failures enter this path. Never infer safety
            # from page text or release a lost/quarantined browser lease.
            try:
                result = self.store.schedule_browser_recovery(task["id"], token, error.reason)
            except ReviewError:
                self.store.recover_expired_tasks()
                raise
            retry = result["status"] == "retry_wait"
            self.readiness.browser_failed(reason=error.reason, retryable=retry,
                                          retry_at=result["available_at"] if retry else None)
            self.store.reconcile_processes()
            return {"worker_status": result["status"], "task_id": task["id"]}
        except BrowserNotReady as error:
            try:
                with self.store.transaction(write=True) as db:
                    current = self.store._leased_task(db, task["id"], token)
                    reason = ("submission_unknown" if json.loads(current["checkpoint"]).get("submission_started")
                              else error.reason)
                    self.store._release_task(db, task["id"], "waiting_for_user", error=reason)
                    if task["kind"] == "apply":
                        pid = task["payload"]["process_id"]
                        db.execute("UPDATE queue SET status='needs_input',updated_at=? WHERE id=?", (now(), pid))
                        db.execute("""UPDATE wf_processes SET block_reason=?,revision=revision+1,
                            updated_at=? WHERE id=?""", (reason, now(), pid))
            except ReviewError:
                self.store.recover_expired_tasks()
            self.readiness.browser_failed(reason=error.reason)
            self.store.reconcile_processes()
            return {"worker_status": "blocked", "task_id": task["id"]}
        except Exception as error:
            # This local lock contention occurs after the browser check returns.
            # Requeue only our still-fenced application, before submission starts.
            if (application_started and isinstance(error, ReviewError)
                    and error.reason == "profile_busy"):
                try:
                    if self._requeue_profile_busy(task, token):
                        _log_failure(error, level=logging.WARNING)
                        return {"worker_status": "profile_busy", "task_id": task["id"]}
                except ReviewError as fence_error:
                    _log_failure(fence_error)
            uncertain = task.get("resource") is not None and isinstance(error, TimeoutError)
            if task["kind"] in {"apply", "explore"}:
                self.readiness.browser_failed(uncertain=uncertain)
            try:
                if uncertain:
                    self.store.quarantine_task(task["id"], token, "browser_work_uncertain")
                else:
                    self.store.fail_task(task["id"], token,
                        f"{type(error).__name__}: Worker execution failed. Check the private worker diagnostics.",
                        retryable=not browser_resource(task.get("resource")) and
                                  not isinstance(error, (ReviewError, NotImplementedError)))
            except ReviewError:
                # A lost lease or already parked task must not be mutated by this worker.
                self.store.recover_expired_tasks()
            self.store.reconcile_processes()
            raise
        return {"worker_status": "completed_dispatch", "task_id": task["id"]}


def main():
    from workflow import WorkflowStore
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Explicit database path; no agent is configured.")
    args = parser.parse_args()
    print(json.dumps(Orchestrator(WorkflowStore(args.db)).run_once(), indent=2))


if __name__ == "__main__":
    main()
