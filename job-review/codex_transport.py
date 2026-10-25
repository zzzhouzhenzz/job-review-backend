"""Run one bounded Codex CLI agent with private inputs and validated JSON output.

The installed CLI loads its normal authentication and plugins. Browser turns
reuse host-managed persistent sessions; other roles stay ephemeral. No model is
selected here. Browser actions can
outlive a killed CLI, so uncertain browser runs must keep their resource lease
quarantined until the browser worker is confirmed stopped.
"""
from __future__ import annotations

import json
import fcntl
import hashlib
import logging
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
import uuid
from worker_power import prevent_idle_sleep

from readiness import BrowserRecoverable, BrowserWorkUncertain, WorkerCapacityUnavailable

LOGGER = logging.getLogger(__name__)
RANK_DISABLED_PLUGINS = ("unified-computer-use", "chrome", "browser", "computer-use", "codex-app-tools")


class CodexTransportError(RuntimeError):
    """A safe error category; never includes prompts, model output or stderr."""


class CodexWorkerStopped(CodexTransportError):
    """Shutdown stopped local work; no browser process was started by this call."""


class SessionOnlyHandoffUnverified(CodexTransportError):
    """A tool-free native resume has not been proved to retain the draft tabs."""


def _json_loads(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON field.")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("Non-finite JSON value.")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def validate_result(value, schema):
    """Validate the small JSON Schema subset used by the worker contracts."""
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "enum", "minItems", "maxItems", "minLength", "maxLength",
               "minimum", "maximum", "description", "title", "$schema"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise ValueError("Unsupported worker output schema.")
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool,
             "integer": int, "number": (int, float), "null": type(None)}
    if isinstance(kind, list):
        if not kind or any(not isinstance(item, str) or item not in types for item in kind):
            raise ValueError("Unsupported worker output schema type.")
        kind = next((item for item in kind if isinstance(value, types[item])
                     and not (item in {"integer", "number"} and isinstance(value, bool))), None)
    if kind not in types or not isinstance(value, types[kind]):
        raise ValueError("Worker result does not match its schema type.")
    if kind in {"integer", "number"} and (isinstance(value, bool)
                                           or (isinstance(value, float) and not math.isfinite(value))):
        raise ValueError("Worker result has an invalid number.")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Worker result does not match its enum.")
    if kind == "object":
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError("Worker result is missing a required field.")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ValueError("Worker result contains an unexpected field.")
        for key, item in value.items():
            if key in props:
                validate_result(item, props[key])
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise ValueError("Worker result has an invalid array length.")
        for item in value:
            validate_result(item, schema["items"])
    elif kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ValueError("Worker result has an invalid text length.")
    elif kind in {"integer", "number"}:
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError("Worker result has an invalid numeric value.")


def _private_write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _private_json(path, *, max_bytes=65536):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > max_bytes:
            raise ValueError("Invalid private transport record.")
        return _json_loads(stream.read())


def _replace_private_json(path, value):
    temporary = path.with_name(path.name + "." + str(uuid.uuid4()) + ".tmp")
    try:
        _private_write(temporary, json.dumps(value, sort_keys=True, allow_nan=False))
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_uuid(value):
    try:
        return value if isinstance(value, str) and str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


class _BrowserSession:
    """Host-only task/session binding; never read identity from task input.

    A persisted in-flight marker fails closed after a crash. A completed private
    receipt and a renewed lease are both required before another turn can resume.
    """
    def __init__(self, directory, lease, *, readiness=False):
        self.task_id = _safe_uuid(getattr(lease, "task_id", None))
        token = getattr(lease, "token", None)
        if not self.task_id or not isinstance(token, str) or not token:
            raise CodexTransportError("Browser work requires a fenced task lease.")
        self.lease_digest = hashlib.sha256(token.encode()).hexdigest()
        self.directory = directory / "_browser_sessions"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise CodexTransportError("Browser session directory must be private.")
        # Chrome clears prior handoff marks whenever a session resumes. A
        # readiness-only turn must not consume an application's live draft.
        suffix = '.readiness.json' if readiness else '.json'
        self.path = self.directory / (self.task_id + suffix)
        other_path = self.directory / (self.task_id + ('.json' if readiness else '.readiness.json'))
        # Keep one lock across both scopes. Session isolation is not permission
        # to run two browser calls for one task or ignore an uncertain old call.
        self.lock = os.open(self.directory / (self.task_id + ".lock"),
                            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.previous = None
        self.thread_id = None
        try:
            if os.fstat(self.lock).st_mode & 0o077:
                raise ValueError("Invalid browser session lock.")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CodexTransportError("Browser task already has an active worker.") from None
            if other_path.exists() or other_path.is_symlink():
                other = _private_json(other_path)
                if (not isinstance(other, dict) or other.get('task_id') != self.task_id
                        or other.get('version') != 1):
                    raise ValueError('Invalid browser session record.')
                if other.get('status') != 'ready':
                    raise BrowserWorkUncertain('Previous browser turn has no safe completion receipt.')
            if self.path.exists() or self.path.is_symlink():
                record = _private_json(self.path)
                if (not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1
                        or record.get("task_id") != self.task_id):
                    raise ValueError("Invalid browser session record.")
                if record.get("status") != "ready":
                    raise BrowserWorkUncertain("Previous browser turn has no safe completion receipt.")
                if record.get('recovery_proof'):
                    from shared_browser_session import verified_record
                    verified_record(directory, record)
                    self.previous, self.thread_id = record, record['thread_id']
                    return
                receipt_name = record.get("receipt")
                if not isinstance(receipt_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}-[0-9a-f-]{36}", receipt_name):
                    raise ValueError("Invalid browser receipt reference.")
                receipt_directory = directory / receipt_name
                if receipt_directory.is_symlink():
                    raise ValueError("Invalid browser receipt directory.")
                receipt = _private_json(receipt_directory / "receipt.json")
                thread_id = _safe_uuid(record.get("thread_id"))
                if (not thread_id or receipt.get("status") != "completed"
                        or receipt.get("task_id") != self.task_id
                        or receipt.get("thread_id") != thread_id
                        or receipt.get("browser") is not True
                        or receipt.get("browser_verified") is not True
                        or receipt.get("turn_completed") is not True
                        or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0
                        or receipt.get("lease_digest") != record.get("lease_digest")
                        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("lease_digest", "")))):
                    raise ValueError("Browser session does not match its host receipt.")
                self.previous, self.thread_id = record, thread_id
        except Exception:
            self.close()
            raise

    def started(self, run):
        _replace_private_json(self.path, {"version": 1, "task_id": self.task_id,
            "status": "active", "thread_id": self.thread_id, "receipt": run.name,
            "lease_digest": self.lease_digest})

    def completed(self, run, thread_id, lease):
        # Keep application checkpoints intact. The private receipt is the audit
        # reference; a second host lease fence prevents publishing stale work.
        if lease.heartbeat() is False:
            raise RuntimeError("Lease lost while saving browser session receipt.")
        _replace_private_json(self.path, {"version": 1, "task_id": self.task_id,
            "status": "ready", "thread_id": thread_id, "receipt": run.name,
            "lease_digest": self.lease_digest})

    def failed_before_tools(self, *, thread_id=None, run=None):
        if self.previous is not None:
            _replace_private_json(self.path, self.previous)
        else:
            self.path.unlink(missing_ok=True)

    def close(self):
        os.close(self.lock)


class _Events:
    """Discard raw events after reading only completion and tool identifiers."""
    def __init__(self):
        self.buffer = b""
        self.size = 0
        self.completed = False
        self.failed = False
        self.turn_failed = False
        self.failure_categories = set()
        self.tools = set()
        self.external_started = False
        self.browser_completed = False
        self.thread_id = None

    def _failure_category(self, event):
        """Map transient error text to fixed labels without retaining the text."""
        error = event.get("error", event)
        if isinstance(error, dict):
            message = error.get("message", "")
            code = error.get("code", "")
        else:
            message, code = str(error), ""
        message = message.lower() if isinstance(message, str) else ""
        known = {"invalid_json_schema", "invalid_request_error", "rate_limit_exceeded",
                 "insufficient_quota", "authentication_error", "context_length_exceeded",
                 "model_not_found", "permission_denied"}
        if isinstance(code, str) and code in known:
            return code
        if "schema" in message and any(word in message for word in ("invalid", "response_format", "unsupported")):
            return "invalid_output_schema"
        if "rate limit" in message or "too many requests" in message:
            return "rate_limit_exceeded"
        if message.startswith("selected model is at capacity."):
            return "model_at_capacity"
        if any(word in message for word in ("usage limit", "quota", "credits")):
            return "quota_exhausted"
        if "context" in message and any(word in message for word in ("length", "window", "too long")):
            return "context_length_exceeded"
        if "model" in message and any(word in message for word in ("not found", "not supported", "does not exist")):
            return "model_unavailable"
        if any(word in message for word in ("authentication", "unauthorized", "401")):
            return "authentication_failed"
        if any(word in message for word in ("connection", "network", "disconnected")):
            return "connection_failed"
        if any(word in message for word in ("permission", "sandbox")):
            return "permission_failed"
        return "codex_turn_failed"

    def add(self, chunk):
        self.size += len(chunk)
        if self.size > 64 * 1024 * 1024:
            raise CodexTransportError("Codex output exceeded the worker limit.")
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > 4 * 1024 * 1024:
                raise CodexTransportError("Codex event exceeded the worker limit.")
            if not line.strip():
                continue
            try:
                event = _json_loads(line)
            except (ValueError, UnicodeError):
                raise CodexTransportError("Codex returned an invalid event stream.") from None
            if not isinstance(event, dict):
                raise CodexTransportError("Codex returned an invalid event stream.")
            if event.get("type") == "thread.started":
                thread_id = _safe_uuid(event.get("thread_id"))
                if thread_id is None or self.thread_id is not None:
                    raise CodexTransportError("Codex returned an invalid session identity.")
                self.thread_id = thread_id
            self.completed |= event.get("type") == "turn.completed"
            self.failed |= event.get("type") in {"turn.failed", "error"}
            self.turn_failed |= event.get("type") == "turn.failed"
            if event.get("type") in {"turn.failed", "error"}:
                self.failure_categories.add(self._failure_category(event))
            item = event.get("item", {})
            if event.get("type") in {"item.started", "item.completed"} and isinstance(item, dict):
                self.external_started |= item.get("type") not in {"agent_message", "reasoning", "todo_list"}
            if (event.get("type") == "item.completed" and isinstance(item, dict)
                    and item.get("type") == "mcp_tool_call"):
                server, tool = item.get("server"), item.get("tool")
                self.external_started = True
                result = item.get("result")
                is_error = (isinstance(result, dict)
                            and (result.get("isError") is True or result.get("is_error") is True))
                if (server == "cua_repl" and tool == "js" and item.get("status") == "completed"
                        and not item.get("error") and not is_error):
                    self.browser_completed = True
                if all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", v)
                       for v in (server, tool)):
                    self.tools.add((server, tool))
        if len(self.buffer) > 4 * 1024 * 1024:
            raise CodexTransportError("Codex event exceeded the worker limit.")


class _Diagnostics:
    """Consume stderr without logging it; retain only fixed error categories."""
    def __init__(self):
        self.buffer = b""
        self.size = 0
        self.categories = set()

    def add(self, chunk):
        self.size += len(chunk)
        self.buffer = (self.buffer + chunk)[-65536:]
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self._classify(line)

    def _classify(self, line):
        message = line.decode("utf-8", errors="replace").lower()
        if "config" in message and any(word in message for word in ("error", "invalid", "failed")):
            self.categories.add("invalid_configuration")
        if "unexpected argument" in message or "unrecognized option" in message:
            self.categories.add("unsupported_cli_option")
        category = _Events()._failure_category({"message": message})
        if category != "codex_turn_failed":
            self.categories.add(category)

    def finish(self):
        if self.buffer:
            self._classify(self.buffer)
        self.buffer = b""


class CodexTransport:
    def __init__(self, binary, run_directory, *, timeout=900, heartbeat_interval=15, cwd=None,
                 browser_transport='exec', browser_session_policy='per_task'):
        self.binary = Path(binary)
        self.run_directory = Path(run_directory)
        self.cwd = Path(cwd) if cwd is not None else Path(__file__).resolve().parent
        if browser_transport not in {'exec', 'app-server'}:
            raise ValueError('Browser transport must be exec or app-server.')
        self.browser_transport = browser_transport
        if browser_session_policy not in {'per_task', 'shared'} or (
                browser_session_policy == 'shared' and browser_transport != 'app-server'):
            raise ValueError('Shared browser sessions require app-server transport.')
        self.browser_session_policy = browser_session_policy
        if (not self.binary.is_absolute() or not self.binary.is_file()
                or not os.access(self.binary, os.X_OK)):
            raise ValueError("Codex binary must be an executable absolute file path.")
        if not self.run_directory.is_absolute() or self.run_directory.is_symlink():
            raise ValueError("Codex run directory must be an absolute private directory.")
        if not self.cwd.is_absolute() or not self.cwd.is_dir():
            raise ValueError("Codex working directory must be an existing absolute directory.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Codex timeout must be positive and finite.")
        if not math.isfinite(heartbeat_interval) or not 0 < heartbeat_interval < 60:
            raise ValueError("Codex heartbeat interval must be less than 60 seconds.")
        self.run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.run_directory.stat().st_mode & 0o077:
            raise ValueError("Codex run directory permissions must be 0700.")
        self.timeout, self.heartbeat_interval = timeout, heartbeat_interval
        self._closed = threading.Event()
        from chrome_access_gate import ChromeAccessGate, CAPACITY, POLICY
        self.chrome_access_capacity, self.chrome_access_policy = CAPACITY, POLICY
        self.chrome_access_gate = ChromeAccessGate(self.run_directory)

    def close(self):
        """Signal every active invocation; each runner terminates its own group."""
        self._closed.set()

    @staticmethod
    def _stop(process):
        # Even when the CLI exited, an MCP subprocess may still hold stdout.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # An MCP child may remain after the CLI exits. Terminate the whole group;
        # this still does not establish that the external browser has stopped.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Do not mask the original uncertain-browser failure if the OS has
            # not yet reaped a process after SIGKILL.
            if process.poll() is None:
                return

    @staticmethod
    def _error(message, browser, started):
        if browser and started:
            return BrowserWorkUncertain(message + " Browser work may still be active.")
        return CodexTransportError(message)

    def _run_browser_appserver(self, run, role, prompt, schema, lease, session, *, require_browser=True):
        from codex_appserver import AppServerError, run_appserver
        result, status, safe_failure = None, 'failed', False
        failure = None
        failed_thread_id = None
        approval = getattr(lease, 'upload_approval', None) if require_browser and role == 'application_prepare' else None
        gate_ticket = getattr(lease, '_chrome_gate_ticket', None) if require_browser else None
        try:
            if gate_ticket:
                gate_ticket.native_started(run)
            session.started(run)
            with prevent_idle_sleep(os.getpid()):
                result = run_appserver(binary=self.binary, cwd=self.cwd, prompt=prompt,
                    schema=schema, thread_id=session.thread_id, timeout=self.timeout,
                    heartbeat_interval=self.heartbeat_interval, heartbeat=lease.heartbeat,
                    closed=self._closed,
                    thread_name=getattr(session, 'name', None),
                    **({} if require_browser else {'require_browser': False}),
                    approve_request=approval or (lambda request: {'action': 'cancel', 'content': None}))
            if (not result.turn_completed or require_browser and not result.browser_verified
                    or not require_browser and (result.browser_verified or result.tools)):
                raise BrowserWorkUncertain('The browser turn has no verified completion.')
            if not _safe_uuid(result.thread_id) or (session.thread_id and result.thread_id != session.thread_id):
                raise BrowserWorkUncertain('Browser session identity changed.')
            validate_result(result.result, schema)
            if lease.heartbeat() is False:
                raise BrowserWorkUncertain('Browser lease was lost after completion.')
            # result.json was created privately before the worker started.
            with (run / 'result.json').open('w', encoding='utf-8') as stream:
                json.dump(result.result, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            status = 'completed'
            return result.result
        except AppServerError as error:
            safe_failure = not error.uncertain
            failed_thread_id = _safe_uuid(error.thread_id)
            # Driver messages are fixed categories. Server rejection details
            # remain only in this owner-private receipt, never server logs.
            failure = {'category': str(error), 'safe_before_turn': safe_failure,
                       'reason': error.reason, 'turn_id': getattr(error, 'turn_id', None),
                       'protocol_method': getattr(error, 'protocol_method', None),
                       'notification_method': getattr(error, 'notification_method', None),
                       'protocol_code': getattr(error, 'protocol_code', None),
                       'protocol_error': getattr(error, 'protocol_error', None)}
            if error.capacity:
                raise WorkerCapacityUnavailable('The selected model is temporarily at capacity.') from None
            if error.safe_stopped:
                status = 'stopped'
                raise CodexWorkerStopped('Codex worker stopped before browser work.') from None
            diagnostic = error.protocol_error
            if (error.uncertain is False and error.turn_id is None
                    and session.thread_id and failed_thread_id == session.thread_id
                    and error.reason == 'protocol_rejected'
                    and error.protocol_method == 'thread/resume'
                    and type(error.protocol_code) is int and error.protocol_code == -32600
                    and isinstance(diagnostic, dict)
                    and type(diagnostic.get('code')) is int and diagnostic['code'] == -32600
                    and diagnostic.get('message') ==
                        f'thread {session.thread_id} already has an active writer'):
                # The native owner refused this resume before a turn started.
                # Keep its binding and let the existing bounded browser retry
                # wait for that owner; never unload it or create another session.
                raise BrowserRecoverable('browser_session_unavailable') from None
            raise self._error('Codex app-server did not complete successfully.', True, error.uncertain) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise BrowserWorkUncertain('The browser result or receipt could not be verified.') from None
        finally:
            try:
                _private_write(run / 'receipt.json', json.dumps({
                    'role': role, 'status': status, 'browser': require_browser, 'transport': 'app-server',
                    'session_worker': not require_browser,
                    'application_worker_id': getattr(lease, 'application_worker_id', None),
                    'task_id': session.task_id, 'thread_id': result.thread_id if result else failed_thread_id,
                    'resumed_thread_id': session.thread_id, 'lease_digest': session.lease_digest,
                    'turn_id': getattr(result, 'turn_id', None),
                    'turn_completed': bool(result and result.turn_completed),
                    'event_bytes': result.event_bytes if result else 0,
                    'exit_code': result.exit_code if result else None,
                    'browser_verified': bool(result and result.browser_verified),
                    'tools': [{'server': server, 'tool': tool} for server, tool in sorted(result.tools)] if result else [],
                    'upload_approvals': approval.decisions if approval else [],
                    'upload_rejections': approval.rejections if approval else [],
                    'approval_events': list(result.approval_events) if result else [],
                    'failure': failure,
                    'chrome_access': gate_ticket.receipt() if gate_ticket else None,
                }, sort_keys=True))
                if status == 'completed':
                    session.completed(run, result.thread_id, lease)
                    # Host object only. Never accept a model-supplied receipt path.
                    lease._last_native_browser_receipt = run / 'receipt.json'
                    if gate_ticket:
                        gate_ticket.verified_terminal()
                elif safe_failure:
                    session.failed_before_tools(thread_id=failed_thread_id, run=run)
                    if gate_ticket:
                        gate_ticket.verified_terminal()
            except Exception:
                raise BrowserWorkUncertain('Browser session completion could not be saved.') from None
            finally:
                session.close()

    def run(self, role, prompt, schema, lease, *, browser=False, session_worker=False):
        # Every host browser turn, including discovery and readiness, enters the
        # same FIFO. Tool-free review/formatting runs outside the Chrome gate.
        if not browser:
            return self._run_unguarded(role, prompt, schema, lease, browser=browser, session_worker=session_worker)
        ticket = self.chrome_access_gate.acquire(lease, role, self._closed, self.heartbeat_interval)
        lease._chrome_gate_ticket = ticket
        uncertain = False
        try:
            return self._run_unguarded(role, prompt, schema, lease, browser=True, session_worker=session_worker)
        except BrowserWorkUncertain:
            uncertain = True
            raise
        finally:
            try:
                ticket.finish(safe=ticket.safe_release or not ticket.started and not uncertain)
            finally:
                del lease._chrome_gate_ticket

    def _run_unguarded(self, role, prompt, schema, lease, *, browser=False, session_worker=False):
        if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", role):
            raise ValueError("Invalid Codex worker role.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 4 * 1024 * 1024:
            raise ValueError("Codex worker prompt is empty or too large.")
        if not isinstance(schema, dict):
            raise ValueError("Codex worker schema must be an object.")
        worker_id = getattr(lease, 'application_worker_id', None)
        if type(session_worker) is not bool or session_worker and browser:
            raise ValueError('Use either browser or session-only mode.')
        if worker_id is not None:
            from pooled_browser_session import WORKERS
            if worker_id not in WORKERS:
                raise ValueError('Invalid application worker identity.')
        if (session_worker or browser and worker_id is not None) and (
                worker_id is None or self.browser_transport != 'app-server'):
            raise ValueError('Pool native sessions require an assigned app-server worker.')
        if worker_id is not None and (browser or session_worker):
            verifier=getattr(lease,'verify_pool_transport',None)
            if not callable(verifier):
                raise SessionOnlyHandoffUnverified('Pool transport requires current host verification.')
            verifier()
        if self._closed.is_set():
            raise CodexWorkerStopped("Codex transport is closed.")
        try:
            if lease.heartbeat() is False:
                raise RuntimeError("Lease lost.")
        except Exception:
            raise CodexTransportError("Codex worker lease renewal failed before startup.") from None

        session = None
        if browser or session_worker:
            try:
                if worker_id is not None:
                    from pooled_browser_session import open_session
                    session = open_session(self, lease, role)
                    if session_worker:
                        session.use_session_only()
                    admission=getattr(lease,'admit_application_tab',None)
                    if not callable(admission):
                        raise CodexTransportError('Pool browser dispatch requires host tab admission.')
                    binding=admission(role,session.thread_id)
                    if binding:
                        prompt += ('\nHost application tab binding: ' + json.dumps({key:binding[key] for key in
                            ('task_id','process_id','worker_id','native_thread_id','tab_id','job_url')})
                            + '\nOnly this exact application tab may be used for application work. '
                            'Do not select another idle draft or create a replacement. If unavailable, stop. '
                            'Mark this same unfinished tab for handoff before ending each browser turn.\n')
                elif self.browser_session_policy == 'shared':
                    from shared_browser_session import open_session
                    session = open_session(self, lease, role)
                else:
                    session = _BrowserSession(self.run_directory, lease, readiness=role == 'linkedin_session')
            except BrowserWorkUncertain:
                if session:
                    session.close()
                raise
            except (OSError, ValueError, KeyError, TypeError):
                if session:
                    session.close()
                raise CodexTransportError("Browser session record could not be verified.") from None
            except BaseException:
                if session:
                    session.close()
                raise
        run = self.run_directory / (role + "-" + str(uuid.uuid4()))
        try:
            if session and hasattr(session, 'preservation_notice'):
                prompt += session.preservation_notice()
            run.mkdir(mode=0o700)
            _private_write(run / "prompt.txt", prompt)
            _private_write(run / "schema.json", json.dumps(schema, allow_nan=False))
            _private_write(run / "result.json", "")
        except Exception:
            if session:
                session.close()
            raise
        if (browser or session_worker) and self.browser_transport == 'app-server':
            return self._run_browser_appserver(run, role, prompt, schema, lease, session,
                                              require_browser=not session_worker)
        command = [str(self.binary), "--no-daemon", "-a", "never", "-c", "features.multi_agent=false",
                   "-c", "features.multi_agent_v2=false"]
        if not browser:
            # Ranking uses local evidence only. Disable both current CUA and
            # legacy browser entry points for this invocation, never globally.
            for plugin in RANK_DISABLED_PLUGINS:
                command.extend(["-c", 'plugins.' + plugin + '@openai-bundled.enabled=false'])
            for server in ("node_repl", "computer-use"):
                command.extend(["-c", 'mcp_servers.' + server + '.enabled=false'])
        command += ["exec", "--sandbox", "read-only"]
        if session and session.thread_id:
            command += ["resume"]
        if not browser:
            command += ["--ephemeral"]
        command += ["--json", "--output-schema", str(run / "schema.json"),
                    "--output-last-message", str(run / "result.json")]
        if session and session.thread_id:
            command += [session.thread_id]
        command += ["-"]
        events, diagnostics, process, started = _Events(), _Diagnostics(), None, False
        gate_ticket = getattr(lease, '_chrome_gate_ticket', None) if browser else None
        status = "failed"
        try:
            with (run / "prompt.txt").open("rb") as input_stream:
                if self._closed.is_set():
                    raise CodexWorkerStopped("Codex worker was cancelled before startup.")
                if session:
                    if gate_ticket:
                        gate_ticket.native_started(run)
                    session.started(run)
                process = subprocess.Popen(command, stdin=input_stream, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, cwd=self.cwd,
                                           start_new_session=True)
            started = True
            begin = heartbeat = time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    current = time.monotonic()
                    if self._closed.is_set():
                        if not browser:
                            raise CodexWorkerStopped("Codex worker was cancelled.")
                        raise self._error("Codex worker was cancelled.", browser, started)
                    if current - begin >= self.timeout:
                        raise self._error("Codex worker timed out.", browser, started)
                    if current - heartbeat >= self.heartbeat_interval:
                        try:
                            if lease.heartbeat() is False:
                                raise RuntimeError("Lease lost.")
                        except Exception:
                            raise self._error("Codex worker lease renewal failed.", browser, started) from None
                        heartbeat = current
                    for key, _ in selector.select(min(0.25, self.heartbeat_interval,
                                                       max(0, self.timeout - (current - begin)))):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if chunk:
                            (events if key.fileobj is process.stdout else diagnostics).add(chunk)
                        else:
                            selector.unregister(key.fileobj)
            diagnostics.finish()
            if process.returncode != 0 or not events.completed or events.failed or events.buffer.strip():
                categories = ", ".join(sorted(events.failure_categories | diagnostics.categories)) or "incomplete_turn"
                # A complete failed turn before any tool started cannot have
                # touched Chrome. Missing/broken event streams remain uncertain.
                no_tool_failure = ((events.failed or diagnostics.categories) and not events.external_started
                                   and not events.buffer.strip())
                if (no_tool_failure and events.turn_failed and not events.completed
                        and events.failure_categories == {'model_at_capacity'}
                        and not diagnostics.categories):
                    raise WorkerCapacityUnavailable('The selected model is temporarily at capacity.')
                raise self._error("Codex worker did not complete successfully (" + categories + ").",
                                  browser, started and not no_tool_failure)
            if browser and not events.browser_completed:
                raise self._error("Codex worker did not verify Chrome through its plugin.",
                                  browser, events.external_started)
            if session and (not events.thread_id or (session.thread_id and events.thread_id != session.thread_id)):
                raise BrowserWorkUncertain("Codex browser session identity did not match its host receipt.")
            result_path = run / "result.json"
            if result_path.is_symlink() or result_path.stat().st_size > 1024 * 1024:
                raise ValueError("Invalid worker result file.")
            result_path.chmod(0o600)
            result = _json_loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("Invalid worker result root.")
            validate_result(result, schema)
            # Recheck the fence before the caller imports any result into SQLite.
            try:
                if lease.heartbeat() is False:
                    raise RuntimeError("Lease lost.")
            except Exception:
                raise self._error("Codex worker lease renewal failed after completion.", browser, started) from None
            status = "completed"
            return result
        except CodexWorkerStopped:
            status = "stopped"
            raise
        except CodexTransportError as error:
            # Explicit structured failures before a tool started are safe to
            # report without claiming there may be a detached browser action.
            if events.completed or ((events.failed or diagnostics.categories) and not events.external_started):
                raise
            raise self._error(str(error), browser, started) from None
        except BrowserWorkUncertain:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise self._error("Codex worker returned an invalid result or could not run.", browser, started) from None
        finally:
            stop_unconfirmed = False
            if process is not None:
                self._stop(process)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                if status == "stopped" and process.poll() is None:
                    stop_unconfirmed = True
                    status = "failed"
            diagnostics.finish()
            try:
                _private_write(run / "receipt.json", json.dumps({
                    "role": role, "status": status, "browser": browser,
                    "task_id": _safe_uuid(getattr(lease, "task_id", None)),
                    "thread_id": events.thread_id,
                    "resumed_thread_id": session.thread_id if session else None,
                    "lease_digest": session.lease_digest if session else None,
                    "turn_completed": events.completed, "event_bytes": events.size,
                    "exit_code": process.returncode if process is not None else None,
                    "failure_categories": sorted(events.failure_categories | diagnostics.categories),
                    "stderr_bytes": diagnostics.size,
                    "browser_verified": events.browser_completed,
                    "tools": [{"server": server, "tool": tool} for server, tool in sorted(events.tools)],
                    "chrome_access": gate_ticket.receipt() if gate_ticket else None,
                }, sort_keys=True))
                if session:
                    if status == "completed":
                        try:
                            session.completed(run, events.thread_id, lease)
                            if gate_ticket:
                                gate_ticket.verified_terminal()
                        except Exception:
                            raise BrowserWorkUncertain("Browser session completion could not be fenced and saved.") from None
                    elif not started or ((events.completed or events.failed or diagnostics.categories)
                                         and not events.external_started and not events.buffer.strip()):
                        session.failed_before_tools()
                        if gate_ticket:
                            gate_ticket.verified_terminal()
            except BrowserWorkUncertain:
                raise
            except OSError:
                if status == "completed":
                    raise self._error("Codex worker receipt could not be saved.", browser, started) from None
                # Preserve the original uncertainty category during cleanup.
                LOGGER.error("Codex worker receipt could not be saved after a failed run.")
            finally:
                if session:
                    session.close()
            if stop_unconfirmed:
                raise CodexTransportError("Codex worker termination could not be confirmed.")
