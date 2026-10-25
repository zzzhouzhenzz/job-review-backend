"""Independent discovery and application execution lanes outside HTTP threads."""
from __future__ import annotations

import logging
import math
import threading
import time

LOGGER = logging.getLogger(__name__)
LANES = {"discovery": ("explore", "rank"), "application": ("apply",)}


class WorkerShutdownTimeout(RuntimeError):
    """The owner must retain its database lock while worker threads are alive."""


class WorkerRunner:
    def __init__(self, orchestrator, *, poll_interval=0.5):
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Worker poll interval must be positive and finite.")
        self.orchestrator = orchestrator
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = {}
        self._last = {}
        self._started = False
        self._runtime_close_started = False

    def start(self):
        with self._lock:
            if self._started:
                return
            if self._stop.is_set():
                raise RuntimeError("A stopped worker runner cannot restart.")
            self._started = True
            if not self.orchestrator.runtime.enabled:
                return
            supported = set(getattr(self.orchestrator.runtime, "supported_kinds", {"explore", "rank", "apply"}))
            lanes = dict(LANES)
            if getattr(self.orchestrator.runtime, 'application_pool_size', 1) == 3:
                lanes.pop('application')
                lanes.update({f'apply-{n}': ('apply',) for n in range(1,4)})
            for lane, kinds in lanes.items():
                if supported.intersection(kinds):
                    self._threads[lane] = threading.Thread(target=self._run, args=(lane, kinds),
                                                          name="job-" + lane + "-worker", daemon=False)
            for thread in self._threads.values():
                thread.start()

    def _run(self, lane, kinds):
        while not self._stop.is_set():
            try:
                if lane.startswith('apply-'):
                    result = self.orchestrator.run_once(kinds=kinds, application_worker_id=lane)
                else:
                    result = self.orchestrator.run_once(kinds=kinds)
            except Exception as error:
                # The orchestrator records the durable outcome. Raw adapter
                # output may contain private page text and stays out of logs.
                LOGGER.error("%s worker dispatch failed (%s)", lane, type(error).__name__)
                result = {"worker_status": "error", "error_type": type(error).__name__}
            with self._lock:
                self._last[lane] = result
            self._stop.wait(self.poll_interval)

    def snapshot(self):
        with self._lock:
            return {lane: {"running": thread.is_alive(), **self._last.get(lane, {})}
                    for lane, thread in self._threads.items()}

    def check_running(self):
        with self._lock:
            if self._started and not self._stop.is_set():
                stopped = [lane for lane, thread in self._threads.items() if not thread.is_alive()]
                if stopped:
                    raise RuntimeError("Worker lanes stopped unexpectedly: " + ", ".join(stopped))

    def request_stop(self):
        """Stop new dispatch and ask the runtime to cancel active calls once."""
        self._stop.set()
        with self._lock:
            if self._runtime_close_started:
                return
            self._runtime_close_started = True
        close = getattr(self.orchestrator.runtime, "close", None)
        if close is not None:
            close()

    def stop(self, timeout=5):
        """Stop taking work and join active calls, or report unresolved threads.

        A timeout does not kill browser activity or authorize releasing the
        supervisor database lock. The caller may retry this join later.
        """
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Worker stop timeout must be nonnegative and finite, or None.")
        close_error = None
        try:
            self.request_stop()
        except Exception as error:
            close_error = error
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads.items())
        for _, thread in threads:
            thread.join(None if deadline is None else max(0, deadline - time.monotonic()))
        alive = [lane for lane, thread in threads if thread.is_alive()]
        if alive:
            raise WorkerShutdownTimeout("Worker lanes remain active; retain the database lock: " + ", ".join(alive))
        wait_closed = getattr(self.orchestrator.runtime, "wait_closed", None)
        if wait_closed is not None:
            wait_closed(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
            if isinstance(close_error, WorkerShutdownTimeout):
                # The runtime's first bounded join timed out, but this join
                # proved that its remaining threads have now stopped.
                close_error = None
        if close_error is not None:
            raise RuntimeError("Worker runtime shutdown failed after its lanes stopped") from close_error

    close = stop
