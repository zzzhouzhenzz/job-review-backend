"""Durable scheduling and dispatch. Production agent execution is intentionally absent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Protocol

from store import ReviewError, now
from readiness import ReadinessMonitor


class AgentRuntime(Protocol):
    """Future runtime owns dedicated sessions, source access, and browser operations.

    Apply must use the fenced ApplicationMixin lifecycle around every side effect.
    It may return only after parking for questions or confirming a receipt. It must
    never use generic task completion to claim that an application was submitted.
    Rank returns complete plain-text card content and ordered selections as
    defined in ranking.py. The iOS client owns layout and rendering.
    """
    enabled: bool

    def preflight(self, probe_id: str, lease) -> dict:
        """Read-only, current Chrome and dedicated-session checks. See CONTRACT_V1.

        Return only after all probe operations stop. Echo the nonce in each
        session response. Cached IDs or process liveness are not ready evidence.
        The runtime must recheck session access before external side effects.
        """
        ...

    def explore(self, context: dict, lease: "TaskLease") -> dict: ...
    def rank(self, context: dict, lease: "TaskLease") -> dict: ...
    def apply(self, store, task: dict) -> None: ...


class UnconfiguredRuntime:
    enabled = False

    def preflight(self, probe_id, lease):
        raise NotImplementedError("Chrome and dedicated agent health probes are not configured.")

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

    def heartbeat(self):
        return self.store.heartbeat_task(self.task_id, self.token)

    def checkpoint(self, value):
        return self.store.checkpoint_task(self.task_id, self.token, value)


class Orchestrator:
    def __init__(self, store, runtime=None, owner="local-worker", *, readiness_options=None):
        self.store = store
        self.runtime = runtime if runtime is not None else UnconfiguredRuntime()
        self.owner = owner
        self.readiness = ReadinessMonitor(store, self.runtime, **(readiness_options or {}))

    def tick(self):
        """Recover local state, then schedule. This method never invokes an agent."""
        recovered = self.store.recover_expired_tasks()
        self.store.reconcile_processes()
        refill = self.store.schedule_refill()
        return {"worker_enabled": bool(self.runtime.enabled), "recovered_tasks": recovered, "refill_task_id": refill}

    def _defer_if_unready(self, task):
        health = self.readiness.snapshot()
        if health["ready"]:
            return None
        with self.store.transaction(write=True) as db:
            self.store._leased_task(db, task["id"], task["lease_token"])
            self.store._release_task(db, task["id"], "queued")
            if task["kind"] == "apply":
                # start_application only prepares local state. No adapter ran.
                db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=? AND status='in_progress'",
                           (now(), task["payload"]["process_id"]))
        return {"worker_status": health["status"], "task_id": None}

    def run_once(self):
        self.tick()
        if not self.runtime.enabled:
            return {"worker_status": "not_configured", "task_id": None}
        self.readiness.poll()
        health = self.readiness.snapshot()
        if not health["ready"]:
            return {"worker_status": health["status"], "task_id": None}
        task = self.store.lease_task(self.owner, kinds=["apply", "rank", "explore"])
        if task is None:
            return {"worker_status": "idle", "task_id": None}
        token = task["lease_token"]
        try:
            # Health can expire while waiting for the SQLite task lease.
            deferred = self._defer_if_unready(task)
            if deferred:
                return deferred
            if task["kind"] == "apply":
                self.store.start_application(task["payload"]["process_id"], token)
                deferred = self._defer_if_unready(task)
                if deferred:
                    return deferred
                self.runtime.apply(self.store, task)
                with self.store.transaction() as db:
                    current = self.store._task(db, task["id"])
                    if current["status"] == "running":
                        raise RuntimeError("Application runtime returned without a receipt or a durable wait state.")
            else:
                context = self.store.discovery_context(task["id"], token)
                deferred = self._defer_if_unready(task)
                if deferred:
                    return deferred
                if task["kind"] == "explore":
                    self.store.accept_exploration(task["id"], token, self.runtime.explore(context, TaskLease(self.store, task)))
                else:
                    self.store.accept_ranking(task["id"], token, self.runtime.rank(context, TaskLease(self.store, task)))
        except Exception as error:
            self.readiness.invalidate()
            try:
                self.store.fail_task(task["id"], token, f"{type(error).__name__}: {error}",
                                     retryable=not isinstance(error, (ReviewError, NotImplementedError)))
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
