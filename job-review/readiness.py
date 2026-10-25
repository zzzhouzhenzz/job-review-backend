"""Storage health and on-demand browser readiness, without startup agent calls.

Browser readiness belongs to a leased explore/apply task. The runtime must use
bounded calls and report uncertain browser work instead of claiming it stopped.
The health report is an observation, never permission for a later browser action.
"""
from __future__ import annotations

import copy
import logging
import math
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from store import now

LOGGER = logging.getLogger(__name__)
CHECK_IDS = ("storage", "chrome_attachment", "linkedin_auth")
RECOVERABLE_BROWSER_REASONS = frozenset({
    "browser_tab_unavailable", "browser_attachment_transient", "browser_session_unavailable",
    "application_page_transient",
})
HUMAN_BROWSER_REASONS = frozenset({
    "login_required", "challenge_required", "tool_approval_required", "file_access_required",
    "browser_action_required",
})


class BrowserNotReady(RuntimeError):
    """The browser check stopped and found a condition requiring user action."""

    def __init__(self, message="The browser needs user action.", *, reason="browser_action_required"):
        if reason not in HUMAN_BROWSER_REASONS:
            raise ValueError("Use a recognized human browser blocker.")
        self.reason = reason
        super().__init__(message)


class BrowserRecoverable(RuntimeError):
    """A stopped browser call reported an explicit, safe-to-recheck failure.

    Never use this exception for a tool denial or an operation that may still be
    active. The next attempt must acquire a new lease and run browser readiness.
    """

    def __init__(self, reason):
        if reason not in RECOVERABLE_BROWSER_REASONS:
            raise ValueError("Use a recognized recoverable browser failure.")
        self.reason = reason
        super().__init__(reason)


class BrowserWorkUncertain(TimeoutError):
    """Browser work may still be active; its resource must remain quarantined."""


class WorkerCapacityUnavailable(RuntimeError):
    """The provider rejected a completed call before any tool started."""
    reason = "model_at_capacity"


def _check(check_id, status, message):
    return {"id": check_id, "status": status, "message": message}


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_browser(result):
    """Validate coherent evidence; only the runtime can observe Chrome itself."""
    result = result if isinstance(result, dict) else {}
    chrome = result.get("chrome")
    linkedin = result.get("linkedin")
    chrome = chrome if isinstance(chrome, dict) else {}
    linkedin = linkedin if isinstance(linkedin, dict) else {}
    session = chrome.get("session_id")
    attached = chrome.get("attached") is True and _nonempty(session)
    signed_in = attached and linkedin.get("authenticated") is True and linkedin.get("session_id") == session
    try:
        url = urlsplit(linkedin.get("url", ""))
        signed_in = bool(signed_in and _nonempty(linkedin.get("tab_id"))
                         and url.scheme == "https" and url.hostname in {"www.linkedin.com", "linkedin.com"}
                         and not url.username and not url.password and url.port in {None, 443})
    except (TypeError, ValueError, AttributeError):
        signed_in = False
    return [
        _check("chrome_attachment", "passed" if attached else "blocked",
               "Chrome is attached." if attached else "Attach Chrome before retrying this task."),
        _check("linkedin_auth", "passed" if signed_in else "blocked",
               "LinkedIn sign-in and the task tab were verified." if signed_in
               else "Sign in to LinkedIn in the attached Chrome session, then retry this task."),
    ]


def validate_application_browser(result,task,lease):
    """External applications prove their own bound tab, not a LinkedIn login."""
    result=result if isinstance(result,dict) else {}
    chrome=result.get('chrome',{});application=result.get('application',{})
    binding=lease.store.application_tab_binding(task['id'],lease.token,lease.application_worker_id,required=True)
    valid=(result.get('scope')=='application' and chrome.get('attached') is True
        and chrome.get('session_id')==binding['browser_id'] and application.get('verified') is True
        and application.get('binding_id')==binding['id'] and application.get('process_id')==binding['process_id']
        and application.get('tab_id')==binding['tab_id'] and application.get('url')==binding['job_url'])
    return [_check('chrome_attachment','passed' if valid else 'blocked',
        'Chrome and this exact application tab were verified.' if valid else 'The exact application tab could not be verified.'),
        _check('linkedin_auth','on_demand','LinkedIn is checked for discovery and LinkedIn actions only.')]


def application_lookup_deferred(result, task, lease):
    """Accept only exact host-fenced affinity, never claim a fresh browser check."""
    if not isinstance(result, dict) or result.get('verification_deferred') is not True:
        return False
    from store import ReviewError
    chrome = result.get('chrome', {})
    application = result.get('application', {})
    binding = lease.store.application_tab_binding(task['id'], lease.token, lease.application_worker_id, required=True)
    if not (task['kind'] == 'apply' and lease.application_worker_id and result.get('scope') == 'application'
            and not result.get('blocker') and not result.get('blocker_reason')
            and chrome.get('attached') is False and application.get('verified') is False
            and chrome.get('session_id') == binding['browser_id']
            and application.get('binding_id') == binding['id']
            and application.get('process_id') == binding['process_id']
            and application.get('tab_id') == binding['tab_id']
            and application.get('url') == binding['job_url']):
        raise ReviewError('Deferred application lookup changed its host binding or claimed readiness.', 409)
    return True


class ReadinessMonitor:
    def __init__(self, store, runtime, *, timeout=30, ttl=300, retry_interval=30, clock=time.monotonic):
        # Keep these options for existing server configuration. Browser timeouts
        # are owned by the runtime; this monitor never starts a browser thread.
        if any(not math.isfinite(value) or value <= 0 for value in (timeout, ttl, retry_interval)):
            raise ValueError("Use positive finite readiness times.")
        self.store, self.runtime = store, runtime
        self.retry_interval, self.ttl, self.clock = retry_interval, ttl, clock
        self._lock = threading.RLock()
        self._thread = None
        self._closed = False
        self._storage_ready = False
        self._retry = 0
        self._expires = 0
        self._health = {"boot_id": str(uuid.uuid4()), "status": "checking" if runtime.enabled else "not_configured", "ready": False,
                        "checked_at": None, "expires_at": None, "retry_at": None,
                        "checks": [_check("storage", "checking", "Checking local storage."),
                                   *self._on_demand()]}

    def _on_demand(self):
        return [_check(key, "on_demand" if self.runtime.enabled else "not_configured",
                       "Checked when a browser task starts." if self.runtime.enabled
                       else "The browser runtime is not configured.") for key in CHECK_IDS[1:]]

    @property
    def storage_ready(self):
        with self._lock:
            return self._storage_ready and not self._closed

    def snapshot(self):
        with self._lock:
            if self._health["ready"] and self.clock() >= self._expires:
                self._health["checks"][1:] = self._on_demand()
                self._health.update(status="idle", ready=False, expires_at=None)
            return copy.deepcopy(self._health)

    def poll(self):
        """Check storage asynchronously. Idle servers never contact Chrome."""
        with self._lock:
            if self._closed or self._thread is not None or self._storage_ready or self.clock() < self._retry:
                return
            self._health.update(status="checking" if self.runtime.enabled else "not_configured", ready=False)
            self._thread = threading.Thread(target=self._run_storage, name="job-storage-health", daemon=True)
            self._thread.start()

    start = poll

    def wait(self, timeout=1):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.snapshot()

    def _check_storage(self):
        with closing(self.store.connect()) as db:
            db.execute("PRAGMA busy_timeout=2000")
            if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise RuntimeError("SQLite quick_check failed")
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO wf_settings(key,value) VALUES (?,?)", ("storage-check:" + str(uuid.uuid4()), "probe"))
            finally:
                db.rollback()

    def _run_storage(self):
        success = False
        try:
            self._check_storage()
            success = True
        except Exception:
            LOGGER.exception("Local storage health check failed")
        finally:
            with self._lock:
                self._storage_ready = success and not self._closed
                enabled = bool(self.runtime.enabled)
                self._health["checks"][0] = _check("storage", "passed" if success else "failed",
                    "The local database can be read and written." if success else "The local database check failed. Check the server log.")
                if not self._closed:
                    self._health.update(status=("idle" if enabled else "not_configured") if success else "blocked",
                                        ready=False, checked_at=now(), expires_at=None)
                self._retry = self.clock() + self.retry_interval
                self._thread = None

    def prepare_browser(self, task, lease):
        """Check this task under its existing browser lease. Never cache authority."""
        from application_pool import browser_resource
        if task["kind"] not in {"explore", "apply"} or not browser_resource(task.get("resource")):
            raise ValueError("Only a browser task can request browser readiness.")
        lease.heartbeat()
        scope_provider=getattr(self.runtime,'browser_readiness_scope',None)
        scope=scope_provider(task,lease) if callable(scope_provider) else 'linkedin'
        if scope not in {'application','linkedin'}:
            raise ValueError('Unknown host browser readiness scope.')
        with self._lock:
            self._health["checks"][1:] = [_check(key, "checking", "Checking Chrome for the current task.")
                                          for key in CHECK_IDS[1:]]
            self._health.update(status="checking", ready=False, checked_at=now(), expires_at=None, retry_at=None,
                                operation_scope=scope)
        try:
            result = self.runtime.prepare_browser(task, lease)
            lease.heartbeat()
            if scope == 'application' and application_lookup_deferred(result, task, lease):
                with self._lock:
                    self._health['checks'][1:] = [
                        _check('chrome_attachment','on_demand','The preparation turn will verify its exact saved tab before acting.'),
                        _check('linkedin_auth','on_demand','LinkedIn is checked for discovery and LinkedIn actions only.')]
                    self._expires = 0
                    self._health.update(status='on_demand', ready=False, checked_at=now(),
                                        expires_at=None, retry_at=None)
                return {'scope':scope, 'verification_deferred':True,
                    'chrome':dict(result['chrome']), 'application':dict(result['application'])}
            checks = validate_application_browser(result,task,lease) if scope=='application' else validate_browser(result)
            if isinstance(result, dict) and (result.get('blocker_reason') or result.get('blocker')):
                checks = [_check(key, 'blocked', 'The browser check reported an unresolved blocker.')
                          for key in CHECK_IDS[1:]]
        except Exception as error:
            self.browser_failed(uncertain=isinstance(error, TimeoutError),
                                reason=getattr(error, "reason", None),
                                retryable=isinstance(error, BrowserRecoverable),scope=scope)
            raise
        ready = all(check['status']=='passed' for check in (checks[:1] if scope=='application' else checks))
        with self._lock:
            self._health["checks"][1:] = checks
            self._expires = self.clock() + self.ttl if ready else 0
            self._health.update(status="ready" if ready else "blocked", ready=ready, checked_at=now(),
                                retry_at=None,
                                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat(timespec="microseconds")
                                if ready else None)
        if not ready:
            reason = result.get("blocker_reason") if isinstance(result, dict) else None
            if isinstance(reason, str) and reason in RECOVERABLE_BROWSER_REASONS:
                self.browser_failed(reason=reason, retryable=True)
                raise BrowserRecoverable(reason)
            raise BrowserNotReady(next(check["message"] for check in checks if check["status"] != "passed"),
                                  reason=reason if isinstance(reason, str) and reason in HUMAN_BROWSER_REASONS
                                  else "browser_action_required")
        if scope=='application':
            return {'scope':scope,'chrome':dict(result['chrome']),'application':dict(result['application'])}
        return {"chrome": {key: result["chrome"][key] for key in ("attached", "session_id")},
                "linkedin": {key: result["linkedin"][key] for key in ("authenticated", "session_id", "url", "tab_id")}}

    def browser_failed(self, *, uncertain=False, reason=None, retryable=False, retry_at=None,scope=None):
        retryable = bool(retryable and not uncertain and reason in RECOVERABLE_BROWSER_REASONS)
        with self._lock:
            scope=scope or self._health.get('operation_scope','linkedin')
            self._health["checks"][1:] = [_check(key, "retry_wait" if retryable else "blocked",
                "Browser work may still be active. Confirm it stopped before retrying."
                if uncertain else "The browser connection will be checked again when this task retries."
                if retryable else "The browser task failed. It will be checked again before another browser task.")
                for key in CHECK_IDS[1:]]
            if scope=='application':
                self._health['checks'][2]=_check('linkedin_auth','on_demand',
                    'LinkedIn is checked for discovery and LinkedIn actions only.')
            self._health.update(status="retry_wait" if retryable else "blocked", ready=False,
                                checked_at=now(), expires_at=None, retry_at=retry_at if retryable else None,
                                operation_scope=scope)

    def invalidate(self):
        self.browser_failed()



    def worker_capacity_failed(self, *, retry_at=None):
        """Do not misreport an unstarted provider call as a Chrome failure."""
        with self._lock:
            self._health['checks'][1:] = self._on_demand()
            self._health.update(status='retry_wait' if retry_at else 'blocked', ready=False,
                                checked_at=now(), expires_at=None, retry_at=retry_at)

    def close(self):
        with self._lock:
            self._closed = True
            self._health.update(status="blocked", ready=False, expires_at=None)
        # The supervisor retains its database lock until this storage operation
        # has returned, including a delayed SQLite transaction.
        self.wait(None)
