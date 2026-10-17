"""Fresh, fail-closed worker readiness. Real Chrome/session probes are adapters.

A successful HTTP response is liveness, not worker readiness. Probe callbacks
must be synchronous: returning asserts that all their browser operations stopped.
Timeout cannot kill a Python thread, so a timed-out callback keeps its browser
resource quarantined until it returns or an operator confirms it stopped.
"""
from __future__ import annotations

import copy
import logging
import math
import re
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from store import now

LOGGER = logging.getLogger(__name__)
CHECK_IDS = ("storage", "chrome_attachment", "linkedin_auth", "linkedin_job_read",
             "explorer_agent", "ranker_agent", "application_agent")
AGENT_CAPABILITIES = {"explorer": {"explore"}, "ranker": {"rank", "flashcard_text"},
                      "application": {"apply", "questions", "receipts"}}


def _check(check_id, status, message):
    return {"id": check_id, "status": status, "message": message}


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_probe(result, probe_id):
    """Validate evidence shape and coherence; only the adapter can observe Chrome.

    No raw session IDs, page text, or adapter error strings enter the public report.
    """
    if not isinstance(result, dict) or result.get("probe_id") != probe_id:
        return [_check(key, "failed", "The probe did not return the current request ID.")
                for key in CHECK_IDS[1:]]
    chrome, linkedin, posting, agents = (result.get(key) for key in ("chrome", "linkedin", "posting", "agents"))
    chrome = chrome if isinstance(chrome, dict) else {}
    linkedin = linkedin if isinstance(linkedin, dict) else {}
    posting = posting if isinstance(posting, dict) else {}
    agents = agents if isinstance(agents, dict) else {}
    session = chrome.get("session_id")
    attached = chrome.get("attached") is True and _nonempty(session)
    authenticated = attached and linkedin.get("authenticated") is True and linkedin.get("session_id") == session
    readable = False
    try:
        url = urlsplit(posting.get("url", ""))
        match = re.fullmatch(r"/jobs/view/(\d+)/?", url.path)
        readable = bool(authenticated and posting.get("session_id") == session
                        and url.scheme == "https" and url.hostname in {"www.linkedin.com", "linkedin.com"}
                        and not url.username and not url.password and url.port in {None, 443}
                        and match and posting.get("job_id") == match.group(1)
                        and posting.get("full_description") is True
                        and all(_nonempty(posting.get(key)) for key in ("title", "company", "description")))
    except (TypeError, ValueError, AttributeError):
        pass  # Invalid adapter evidence becomes an explicit failed check below.
    values = [
        ("chrome_attachment", attached, "Chrome is attached.", "Attach the signed-in Chrome session."),
        ("linkedin_auth", authenticated, "LinkedIn sign-in was verified.", "Verify LinkedIn sign-in in the attached Chrome session."),
        ("linkedin_job_read", readable, "A complete LinkedIn job posting was read.", "The attached session could not verify a complete job posting."),
    ]
    session_ids = [entry.get("session_id") for entry in agents.values() if isinstance(entry, dict)]
    for role, required in AGENT_CAPABILITIES.items():
        agent = agents.get(role)
        agent = agent if isinstance(agent, dict) else {}
        capabilities = agent.get("capabilities")
        healthy = (agent.get("ready") is True and agent.get("probe_id") == probe_id
                   and _nonempty(agent.get("session_id")) and session_ids.count(agent.get("session_id")) == 1
                   and isinstance(capabilities, list) and all(isinstance(item, str) for item in capabilities)
                   and required.issubset(capabilities))
        values.append((role + "_agent", healthy, f"The {role} session is ready.",
                       f"The dedicated {role} session did not confirm its required capabilities."))
    return [_check(key, "passed" if passed else "failed", success if passed else failure)
            for key, passed, success, failure in values]


class ProbeLease:
    """Only lease renewal is exposed to a read-only preflight adapter."""
    def __init__(self, store, task):
        self._store, self._task = store, task

    def heartbeat(self):
        self._store.heartbeat_task(self._task["id"], self._task["lease_token"])


class ReadinessMonitor:
    def __init__(self, store, runtime, *, timeout=30, ttl=300, retry_interval=30, clock=time.monotonic):
        if any(not math.isfinite(value) or value <= 0 for value in (timeout, ttl, retry_interval)) or timeout > 3500:
            raise ValueError("Use positive finite readiness times and a timeout at most 3500 seconds.")
        self.store, self.runtime = store, runtime
        self.timeout, self.ttl, self.retry_interval, self.clock = timeout, ttl, retry_interval, clock
        self._lock = threading.RLock()
        self._thread = self._timer = self._task = None
        self._probe_id = None
        self._deadline = self._expires = self._retry = 0
        self._closed = False
        self._discard = False
        self._health = {"boot_id": str(uuid.uuid4()), "status": "checking" if runtime.enabled else "not_configured",
                        "ready": False, "checked_at": None, "expires_at": None, "retry_at": None,
                        "checks": [_check(key, "checking" if key == "storage" or runtime.enabled else "not_configured",
                                           "Waiting for startup checks." if key == "storage" or runtime.enabled
                                           else "The Chrome and agent runtime is not configured.") for key in CHECK_IDS]}

    def _set_retry(self):
        self._retry = self.clock() + self.retry_interval
        self._health["retry_at"] = (datetime.now(timezone.utc) + timedelta(seconds=self.retry_interval)).isoformat(timespec="microseconds")

    def _expire(self):
        if self._health["ready"] and self.clock() >= self._expires:
            self._health.update(status="stale", ready=False)
            self._health["checks"] = [_check(key, "stale", "The readiness check has expired.") for key in CHECK_IDS]
            self._retry = 0

    def snapshot(self):
        with self._lock:
            self._expire()
            value = copy.deepcopy(self._health)
            if not self.runtime.enabled and value["ready"]:
                value.update(status="not_configured", ready=False)
            return value

    def poll(self):
        """Start at most one asynchronous attempt; never wait on an adapter."""
        with self._lock:
            self._expire()
            if (self._closed or self._thread is not None or self._health["ready"] or self.clock() < self._retry):
                return
            self._probe_id = str(uuid.uuid4())
            self._discard = False
            self._deadline = self.clock() + self.timeout
            self._health.update(status="checking" if self.runtime.enabled else "not_configured", ready=False,
                                expires_at=None, retry_at=None)
            self._health["checks"] = [_check(key, "checking", "The startup check is running.") for key in CHECK_IDS]
            self._timer = threading.Timer(self.timeout, self._timed_out, args=(self._probe_id,))
            self._timer.daemon = True
            self._thread = threading.Thread(target=self._run, args=(self._probe_id,), name="job-readiness", daemon=True)
            self._timer.start()
            self._thread.start()

    start = poll

    def wait(self, timeout=1):
        """Bounded join for tests and explicit local diagnostics, never HTTP reads."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.snapshot()

    def _timed_out(self, probe_id):
        with self._lock:
            if probe_id != self._probe_id or self._thread is None:
                return
            self._health.update(status="timed_out", ready=False, checked_at=now(), expires_at=None)
            self._health["checks"] = [entry if entry["status"] == "passed" else
                                      _check(entry["id"], "timed_out", "The check timed out. Execution remains blocked.")
                                      for entry in self._health["checks"]]
            self._set_retry()
            task = self._task
        if task is not None:
            try:
                self.store.timeout_preflight(task["id"], task["lease_token"])
            except Exception:
                LOGGER.exception("Could not quarantine timed-out readiness probe %s", probe_id)

    def _check_storage(self):
        with closing(self.store.connect()) as db:
            db.execute("PRAGMA busy_timeout=2000")
            if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise RuntimeError("SQLite quick_check failed")
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO wf_settings(key,value) VALUES (?,?)", ("preflight:" + str(uuid.uuid4()), "probe"))
            finally:
                db.rollback()

    def _active(self, probe_id):
        return (probe_id == self._probe_id and not self._closed and not self._discard and self.clock() < self._deadline
                and self._health["status"] != "timed_out")

    def _run(self, probe_id):
        task, checks, success = None, None, False
        status = "blocked"
        try:
            self._check_storage()
            with self._lock:
                self._health["checks"][0] = _check("storage", "passed", "The local database can be read and written.")
                if not self._active(probe_id):
                    return
            if not self.runtime.enabled:
                status = "not_configured"
                checks = [_check(key, status, "The Chrome and agent runtime is not configured.") for key in CHECK_IDS[1:]]
            else:
                task = self.store.lease_preflight("readiness:" + self._health["boot_id"], probe_id,
                                                  lease_seconds=math.ceil(self.timeout) + 5)
                with self._lock:
                    self._task = task
                    if not self._active(probe_id):
                        return
                if task is None:
                    checks = [_check(key, "blocked", "The browser is busy or quarantined. The check will retry.") for key in CHECK_IDS[1:]]
                else:
                    result = self.runtime.preflight(probe_id, ProbeLease(self.store, task))
                    checks = validate_probe(result, probe_id)
                    success = all(entry["status"] == "passed" for entry in checks)
                    status = "ready" if success else "blocked"
        except Exception:
            LOGGER.exception("Readiness probe %s failed", probe_id)
            with self._lock:
                storage_passed = self._health["checks"][0]["status"] == "passed"
                if not storage_passed:
                    self._health["checks"][0] = _check("storage", "failed", "The local database check failed. Check the server log.")
            checks = [_check(key, "failed" if storage_passed else "blocked",
                             "The runtime probe failed. Check the server log." if storage_passed
                             else "The database must be healthy before runtime checks.") for key in CHECK_IDS[1:]]
        finally:
            # A callback return is positive stop evidence, even after timeout.
            if task is not None:
                with self._lock:
                    success = success and self._active(probe_id)
                if success:
                    try:
                        with self.store.transaction() as db:
                            self.store._leased_task(db, task["id"], task["lease_token"])
                    except Exception:
                        LOGGER.exception("Readiness probe %s lost its lease", probe_id)
                        status, success = "blocked", False
                        checks = [_check(key, "blocked", "The probe lost its browser lease. A new check is required.") for key in CHECK_IDS[1:]]
                try:
                    stopped = self.store.stop_preflight(task["id"], task["lease_token"], success=success,
                                                        error=None if success else "Readiness checks did not pass.")
                    if success and stopped["status"] != "succeeded":
                        raise RuntimeError("Probe no longer owned its browser resource")
                except Exception:
                    LOGGER.exception("Could not release stopped readiness probe %s", probe_id)
                    status, success = "blocked", False
                    checks = [_check(key, "blocked", "The probe resource could not be released. Check the server log.") for key in CHECK_IDS[1:]]
            with self._lock:
                if self._active(probe_id) and checks is not None:
                    self._health["checks"][1:] = checks
                    self._health.update(status=status, ready=success, checked_at=now())
                    if success:
                        self._expires = self.clock() + self.ttl
                        self._health["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat(timespec="microseconds")
                    else:
                        self._set_retry()
                elif self._health["status"] != "timed_out" and not self._closed and not self._discard:
                    self._health.update(status="timed_out", ready=False, checked_at=now(), expires_at=None)
                    self._set_retry()
                self._timer.cancel()
                self._task = self._thread = None

    def invalidate(self):
        with self._lock:
            self._discard = True
            self._health.update(status="blocked", ready=False, expires_at=None)
            self._health["checks"] = [_check(key, "blocked", "A worker failed. A new readiness check is required.") for key in CHECK_IDS]
            self._set_retry()

    def close(self):
        """Stop scheduling; a live adapter must return before its lock is released."""
        with self._lock:
            self._closed = True
            probe_id = self._probe_id
            self._health.update(status="blocked", ready=False)
        if probe_id is not None:
            self._timed_out(probe_id)
        self.wait(0.1)
