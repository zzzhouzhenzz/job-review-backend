"""Bounded stdio app-server transport for one browser turn.

The host supplies an authorization callback; the model cannot approve its own
requests. Raw RPC data stays in memory and never appears in error messages.
The caller still validates ``result`` against its worker contract and owns the
durable task lease, session binding, receipts, and browser quarantine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid


LOGGER = logging.getLogger(__name__)
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024 * 1024
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
APPROVAL_POLICY = {"granular": {"mcp_elicitations": True, "rules": False,
    "sandbox_approval": False, "request_permissions": False, "skill_approval": False}}
_PASSIVE_ITEMS = {"userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"}
_THREAD_SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgent",
    "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_NOTIFICATION_METHODS = {"thread/started", "thread/tokenUsage/updated", "thread/status/changed",
    "thread/name/updated", "thread/archived",
    "turn/started", "turn/completed", "item/started", "item/completed", "error",
    "serverRequest/resolved", "mcpServer/startupStatus/updated", "item/agentMessage/delta"}


@dataclass(frozen=True)
class AppServerResult:
    result: dict
    thread_id: str
    turn_completed: bool
    browser_verified: bool
    tools: tuple
    event_bytes: int
    exit_code: int
    approval_events: tuple = ()
    turn_id: str | None = None


class AppServerError(RuntimeError):
    """Safe message/flags; protocol_error is private and must never be logged."""
    def __init__(self, message, *, uncertain=False, capacity=False, safe_stopped=False,
                 thread_id=None, reason="transport_failed", protocol_method=None,
                 protocol_code=None, protocol_error=None, notification_method=None,
                 turn_id=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.capacity = capacity
        self.safe_stopped = safe_stopped
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.reason = reason
        self.protocol_method = protocol_method
        self.protocol_code = protocol_code
        # Only RPC rejections populate this bounded diagnostic. Hosts may save
        # it to an existing 0600 receipt, but not to user messages or logs.
        self.protocol_error = protocol_error
        self.notification_method = notification_method


def _loads(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate field.")
            value[key] = item
        return value

    def constant(_):
        raise ValueError("Non-finite JSON.")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _identity(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _thread_status(value):
    kind = value.get("type") if isinstance(value, dict) else None
    return (isinstance(kind, str) and kind in {"notLoaded", "idle", "systemError", "active"}
            and (kind != "active" or isinstance(value.get("activeFlags"), list)
                 and all(isinstance(flag, str) and flag in {"waitingOnApproval", "waitingOnUserInput"}
                         for flag in value["activeFlags"])))


def _stop(process):
    # Killing this group does not prove that an external browser action stopped.
    process.poll()  # Reap an exited child before signaling its former group.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS can report EPERM even after the owned child was reaped.
            # Its exit code proves local server shutdown; it does not prove an
            # external browser action stopped. Preserve that separate fence.
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise AppServerError("App-server process termination could not be verified.",
                                         uncertain=True, reason="cleanup_failed") from None
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class _Driver:
    def __init__(self, process, *, thread_id, timeout, heartbeat_interval, heartbeat,
                 closed, approve_request, require_browser=True):
        self.process = process
        self.thread_id = thread_id
        self.turn_id = None
        self.turn_sent = False
        self.turn_observed = False
        self.turn_done = None
        self.terminal_error = False
        self.external_started = False
        self.browser_verified = False
        self.tools = set()
        self.final_messages = {}
        self.failures = set()
        self.event_bytes = 0
        self.stderr_bytes = 0
        self.stdout_buffer = b""
        self.stderr_buffer = b""
        self.stdout_eof = False
        self.stderr_eof = False
        self.outgoing = bytearray()
        self.responses = {}
        self.pending = set()
        self.request_ids = set()
        self.approval_events = []
        self.child_threads = set()
        self.child_turns = {}
        self.notification_method = None
        self.serial = 0
        self.deadline = time.monotonic() + timeout
        self.heartbeat_interval = heartbeat_interval
        self.next_heartbeat = time.monotonic()
        self.heartbeat = heartbeat
        self.closed = closed
        self.approve_request = approve_request
        self.require_browser = require_browser
        self.selector = selectors.DefaultSelector()
        for stream, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, kind)
        os.set_blocking(process.stdin.fileno(), False)

    def fail(self, message, *, capacity=False, safe_stopped=False, reason="protocol_failure"):
        raise AppServerError(message, uncertain=(self.turn_sent or self.external_started) and not capacity,
                             capacity=capacity, safe_stopped=safe_stopped,
                             thread_id=self.thread_id, reason=reason,
                             notification_method=self.notification_method, turn_id=self.turn_id)

    def check(self, *, renew=False):
        stopped = self.closed.is_set() if hasattr(self.closed, "is_set") else self.closed()
        if stopped:
            self.fail("App-server worker stopped.",
                      safe_stopped=not (self.turn_sent or self.external_started), reason="stopped")
        now = time.monotonic()
        if now >= self.deadline:
            self.fail("App-server worker timed out.", reason="timeout")
        if renew or now >= self.next_heartbeat:
            try:
                valid = self.heartbeat()
            except Exception:
                self.fail("App-server task lease could not be renewed.", reason="lease_lost")
            if valid is False:
                self.fail("App-server task lease expired.", reason="lease_lost")
            self.next_heartbeat = now + self.heartbeat_interval

    def send(self, value):
        raw = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(raw) + len(self.outgoing) > MAX_INPUT_BYTES:
            self.fail("App-server input exceeded the worker limit.")
        if not self.outgoing:
            self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        self.outgoing.extend(raw)

    def request(self, method, params):
        self.serial += 1
        request_id = "job-review-" + str(self.serial)
        self.pending.add(request_id)
        self.send({"id": request_id, "method": method, "params": params})
        while request_id not in self.responses:
            self.pump()
        response = self.responses.pop(request_id)
        self.pending.remove(request_id)
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int:
                self.fail("App-server returned an invalid protocol error.")
            # A rejected turn/start did not create a turn. This specific server
            # response is safe to retry only before any turn activity; transport
            # loss after sending the request remains uncertain.
            rejected_before_turn = (method == "turn/start" and not self.turn_observed
                                    and self.turn_id is None and not self.external_started
                                    and self.turn_done is None)
            diagnostic = error if len(json.dumps(error, allow_nan=False).encode()) <= 65536 else None
            raise AppServerError("App-server rejected a protocol request.",
                uncertain=(self.turn_sent or self.external_started) and not rejected_before_turn,
                thread_id=self.thread_id, reason="protocol_rejected",
                protocol_method=method, protocol_code=error["code"], protocol_error=diagnostic,
                turn_id=self.turn_id)
        result = response.get("result")
        if not isinstance(result, dict):
            self.fail("App-server returned an invalid protocol response.")
        return result

    def scope(self, params, *, turn=False):
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
            self.fail("App-server returned a different thread identity.")
        if turn and (self.turn_id is None or params.get("turnId") != self.turn_id):
            self.fail("App-server returned a different turn identity.")

    def bind_turn(self, turn):
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not _identity(turn_id) or (self.turn_id is not None and self.turn_id != turn_id):
            self.fail("App-server returned an invalid turn identity.")
        self.turn_id = turn_id

    def item(self, item, *, completed):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            self.fail("App-server returned an invalid item.")
        kind = item["type"]
        if not self.require_browser and kind not in _PASSIVE_ITEMS:
            self.external_started = True
            self.fail("Session-only worker attempted a tool or external action.")
        self.link_children(item, self.thread_id)
        self.external_started |= kind not in _PASSIVE_ITEMS
        if kind == "mcpToolCall":
            server, tool = item.get("server"), item.get("tool")
            if all(isinstance(v, str) and _SAFE_NAME.fullmatch(v) for v in (server, tool)):
                self.tools.add((server, tool))
            result = item.get("result")
            is_error = isinstance(result, dict) and (result.get("isError") is True
                                                      or result.get("is_error") is True)
            if (completed and server == "cua_repl" and tool == "js"
                    and item.get("status") == "completed" and not item.get("error")
                    and isinstance(result, dict) and not is_error):
                self.browser_verified = True
        elif completed and kind == "agentMessage" and item.get("phase") in (None, "final_answer"):
            item_id, text = item.get("id"), item.get("text")
            if not isinstance(item_id, str) or not isinstance(text, str):
                self.fail("App-server returned an invalid final message.")
            if item_id in self.final_messages and self.final_messages[item_id] != text:
                self.fail("App-server changed a completed final message.")
            self.final_messages[item_id] = text

    def failure(self, error):
        message = error.get("message") if isinstance(error, dict) else None
        capacity = (isinstance(message, str)
                    and message.lower().startswith("selected model is at capacity.")
                    and error.get("codexErrorInfo") in (None, "other")
                    and not error.get("additionalDetails")
                    and not error.get("misalignment"))
        self.failures.add("capacity" if capacity else "other")

    def server_request(self, message):
        request_id = message["id"]
        if (type(request_id) not in (str, int) or request_id in self.request_ids
                or len(self.request_ids) >= 1024):
            self.fail("App-server returned an invalid server request identity.")
        self.request_ids.add(request_id)
        method, params = message["method"], message.get("params")
        if not self.require_browser:
            self.send({"id": request_id, "error": {"code": -32601,
                       "message": "Session-only workers cannot request actions."}})
            self.external_started = True
            self.fail("Session-only worker requested an external action.")
        is_child = isinstance(params, dict) and params.get("threadId") in self.child_threads
        if self.turn_done is not None and not is_child:
            self.fail("App-server requested an action after turn completion.")
        # The request-id bound above also bounds these diagnostics to 1024
        # fixed records. Never retain IDs, messages, URLs, schemas, or metadata.
        event = {"method": "unsupported", "mode": "unsupported",
                 "host_callback": False, "action": "cancel"}
        self.approval_events.append(event)
        if method != "mcpServer/elicitation/request":
            self.send({"id": request_id, "error": {"code": -32601,
                       "message": "This client does not support that request."}})
            if not is_child and isinstance(params, dict) and "threadId" in params:
                self.scope(params, turn="turnId" in params)
            return
        event["method"] = "mcp_elicitation"
        if isinstance(params, dict):
            event["mode"] = {"form": "form", "openai/form": "openai_form",
                             "openaiForm": "legacy_openai_form", "url": "url",
                             "openai/userVerification": "user_verification"}.get(
                                 params.get("mode"), "unsupported")
        cancel = {"action": "cancel", "content": None}
        if is_child:
            # A child cannot borrow the parent's exact-job upload consent.
            self.send({"id": request_id, "result": cancel})
            return
        if (not isinstance(params, dict) or params.get("threadId") != self.thread_id
                or not self.turn_id or params.get("turnId") != self.turn_id):
            self.send({"id": request_id, "result": cancel})
            self.fail("App-server elicitation has no matching active turn.")
        self.external_started = True
        answer = cancel
        # Device verification requires a real user proof, and URL flows require
        # an interactive client. Neither reaches the host approval callback.
        if params.get("mode") in {"form", "openai/form", "openaiForm"}:
            self.check(renew=True)
            event["host_callback"] = True
            try:
                candidate = self.approve_request(params)
            except Exception:
                self.send({"id": request_id, "result": cancel})
                self.fail("App-server authorization policy failed.")
            self.check(renew=True)
            if (isinstance(candidate, dict) and set(candidate) <= {"action", "content", "_meta"}
                    and candidate.get("action") in {"accept", "decline", "cancel"}
                    and ("_meta" not in candidate or candidate["_meta"] == {"persist": "session"})):
                if candidate["action"] == "accept" and isinstance(candidate.get("content"), dict):
                    answer = candidate
                elif candidate["action"] != "accept":
                    answer = {"action": candidate["action"], "content": None}
        event["action"] = answer["action"]
        self.send({"id": request_id, "result": answer})

    def register_child(self, identity, *, active=True):
        if not _identity(identity) or identity == self.thread_id:
            self.fail("App-server returned an invalid child thread relationship.")
        if identity not in self.child_threads and len(self.child_threads) >= 64:
            self.fail("App-server child thread limit exceeded.")
        self.child_threads.add(identity)
        self.external_started |= active

    def preload_children(self):
        """Read native relationships before resume can emit helper events.

        The host must first validate its prior completed receipt. notLoaded
        alone is not a stopped-work proof and never releases quarantine here.
        """
        parents, seen, pages = [self.thread_id], {self.thread_id}, 0
        for parent in parents:
            cursor, cursors = None, set()
            while True:
                # At most four pages per parent, and 128 pages overall. A
                # partial graph cannot authorize routing of helper actions.
                if len(cursors) >= 4 or pages >= 128:
                    self.fail("App-server child thread pagination limit exceeded.")
                params = {"parentThreadId": parent, "limit": 64,
                          "sourceKinds": _THREAD_SOURCE_KINDS}
                if cursor is not None:
                    params["cursor"] = cursor
                page = self.request("thread/list", params)
                pages += 1
                children = page.get("data")
                if (not isinstance(children, list) or len(children) > 64
                        or "nextCursor" not in page):
                    self.fail("App-server returned an incomplete child thread graph.")
                for child in children:
                    identity = child.get("id") if isinstance(child, dict) else None
                    if (not _identity(identity) or identity in seen
                            or child.get("parentThreadId") != parent):
                        self.fail("App-server returned an invalid child thread relationship.")
                    status = child.get("status")
                    if not _thread_status(status) or status["type"] not in {"notLoaded", "idle"}:
                        # A previously active helper can outlive the old local
                        # transport. Keep the browser quarantined on this path.
                        self.external_started = True
                        self.fail("App-server child thread is active or its status is unverified.")
                    self.register_child(identity, active=False)
                    seen.add(identity)
                    parents.append(identity)
                cursor = page["nextCursor"]
                if cursor is None:
                    break
                if (not isinstance(cursor, str) or not 0 < len(cursor) <= 4096
                        or cursor in cursors or not children):
                    self.fail("App-server returned an incomplete child thread graph.")
                cursors.add(cursor)

    def link_children(self, item, source):
        """Use native parent-scoped relationship fields, never message text."""
        if item.get("type") == "subAgentActivity":
            target = item.get("agentThreadId")
            # A known helper can send a message to its parent. This native
            # activity reports the recipient; it does not create a child.
            if (target == self.thread_id and source in self.child_threads
                    and item.get("kind") == "interacted"):
                return
            self.register_child(target)
        elif item.get("type") == "collabAgentToolCall":
            targets = item.get("receiverThreadIds")
            if item.get("senderThreadId") != source or not isinstance(targets, list):
                self.fail("App-server returned an invalid child thread relationship.")
            for target in targets:
                self.register_child(target)

    def child_notification(self, method, params):
        if not isinstance(params, dict):
            return False
        identity = params.get("threadId")
        if method == "thread/started":
            thread = params.get("thread")
            if not isinstance(thread, dict):
                return False
            identity = thread.get("id")
            parent = thread.get("parentThreadId")
            if (self.thread_id is not None and identity != self.thread_id and isinstance(parent, str)
                    and (parent == self.thread_id or parent in self.child_threads)):
                self.register_child(identity)
        if not isinstance(identity, str) or identity not in self.child_threads:
            return False
        if not self.require_browser and method in {'turn/started', 'turn/completed', 'item/started', 'item/completed'}:
            self.external_started = True
            self.fail('Session-only worker observed unexpected child activity.')
        # Multiplexed child work is drained separately. It can never establish
        # the parent's browser verification, final JSON, completion, or consent.
        if method in {"turn/started", "turn/completed"}:
            self.external_started = True
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if (not _identity(turn_id) or self.child_turns.get(identity, turn_id) != turn_id):
                self.fail("App-server returned an invalid child turn identity.")
            if method == "turn/started":
                self.child_turns[identity] = turn_id
            else:
                self.child_turns.pop(identity, None)
        elif method in {"item/started", "item/completed"}:
            self.external_started = True
            turn_id, item = params.get("turnId"), params.get("item")
            if (not _identity(turn_id) or self.child_turns.get(identity, turn_id) != turn_id
                    or not isinstance(item, dict)):
                self.fail("App-server returned an invalid child item identity.")
            self.child_turns[identity] = turn_id
            self.link_children(item, identity)
        return True

    def notification(self, method, params):
        if method in {'thread/name/updated', 'thread/archived'}:
            if not isinstance(params, dict) or params.get('threadId') not in {self.thread_id, *self.child_threads}:
                self.fail('App-server returned an unrelated thread notification.')
            if method == 'thread/name/updated' and params.get('threadName') is not None and not isinstance(params['threadName'], str):
                self.fail('App-server returned an invalid thread name.')
            return
        if method == "thread/status/changed":
            # Runtime status can arrive for a historical helper before resume
            # reestablishes its parent link. It carries no turn, tool, approval,
            # or completion evidence. Validate it, then discard it without
            # registering a child or changing any worker execution state.
            status = params.get("status") if isinstance(params, dict) else None
            if (not isinstance(params, dict) or not _identity(params.get("threadId"))
                    or not _thread_status(status)):
                self.fail("App-server returned an invalid thread status notification.")
            return
        if self.child_notification(method, params):
            return
        if method == "thread/tokenUsage/updated":
            # Resume emits the previous turn's usage before a new turn exists.
            # This metric provides no action, approval, or completion evidence.
            self.scope(params)
            return
        if method == "thread/started":
            identity = params.get("thread", {}).get("id") if isinstance(params, dict) else None
            if not _identity(identity) or (self.thread_id is not None and identity != self.thread_id):
                self.fail("App-server returned a different thread identity.")
            self.thread_id = identity
        elif method in {"turn/started", "turn/completed"}:
            self.turn_observed = True
            self.scope(params)
            if not self.turn_sent:
                self.fail("App-server emitted an unexpected turn.")
            turn = params.get("turn")
            self.bind_turn(turn)
            if method == "turn/completed":
                if self.turn_done is not None:
                    self.fail("App-server completed a turn more than once.")
                for item in turn.get("items", []):
                    self.item(item, completed=True)
                self.turn_done = turn.get("status")
                self.terminal_error = turn.get("error") is not None
                if self.turn_done != "completed" or self.terminal_error:
                    self.failure(turn.get("error"))
        elif method in {"item/started", "item/completed", "error"}:
            self.scope(params, turn=True)
            if self.turn_done is not None:
                self.fail("App-server emitted turn work after completion.")
            if method == "error":
                self.failure(params.get("error"))
            else:
                self.item(params.get("item"), completed=method == "item/completed")
        elif isinstance(params, dict) and "threadId" in params:
            self.scope(params, turn="turnId" in params and params["turnId"] is not None)

    def event(self, raw):
        try:
            value = _loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            self.fail("App-server returned invalid JSON.")
        if not isinstance(value, dict) or value.get("jsonrpc", "2.0") != "2.0":
            self.fail("App-server returned an invalid protocol message.")
        if "method" in value:
            if not isinstance(value["method"], str) or "result" in value or "error" in value:
                self.fail("App-server returned an invalid protocol message.")
            if "id" in value:
                self.server_request(value)
            else:
                method = value["method"]
                self.notification_method = method if method in _NOTIFICATION_METHODS else "unsupported"
                try:
                    self.notification(method, value.get("params", {}))
                finally:
                    self.notification_method = None
        else:
            request_id = value.get("id")
            if (not isinstance(request_id, str) or request_id not in self.pending
                    or request_id in self.responses
                    or ("result" in value) == ("error" in value)):
                self.fail("App-server returned an unexpected protocol response.")
            self.responses[request_id] = value

    def diagnostics(self, raw):
        self.stderr_bytes += len(raw)
        if self.stderr_bytes > MAX_EVENT_BYTES:
            self.fail("App-server diagnostics exceeded the worker limit.")
        self.stderr_buffer += raw
        while b"\n" in self.stderr_buffer:
            line, self.stderr_buffer = self.stderr_buffer.split(b"\n", 1)
            self.classify_diagnostic(line)
        if len(self.stderr_buffer) > MAX_LINE_BYTES:
            self.fail("App-server diagnostic line exceeded the worker limit.")

    def classify_diagnostic(self, raw):
        line = raw.decode("utf-8", errors="replace").strip().lower()
        if line.startswith("selected model is at capacity."):
            self.failures.add("capacity")
        elif re.search(r"\b(error|failed|exception|invalid|denied)\b", line):
            self.failures.add("other")

    def pump(self, *, allow_eof=False):
        self.check()
        for key, _ in self.selector.select(min(0.25, self.heartbeat_interval,
                                               max(0, self.deadline - time.monotonic()))):
            if key.data == "stdin":
                size = os.write(key.fd, self.outgoing[:65536])
                del self.outgoing[:size]
                if not self.outgoing:
                    self.selector.unregister(key.fileobj)
                continue
            raw = os.read(key.fd, 65536)
            if not raw:
                self.selector.unregister(key.fileobj)
                if key.data == "stdout":
                    self.stdout_eof = True
                else:
                    self.stderr_eof = True
                if key.data == "stdout" and self.turn_done is None and not allow_eof:
                    self.fail("App-server closed before completing the turn.")
                continue
            if key.data == "stderr":
                self.diagnostics(raw)
                continue
            self.event_bytes += len(raw)
            if self.event_bytes > MAX_EVENT_BYTES:
                self.fail("App-server events exceeded the worker limit.")
            self.stdout_buffer += raw
            while b"\n" in self.stdout_buffer:
                line, self.stdout_buffer = self.stdout_buffer.split(b"\n", 1)
                if len(line) > MAX_LINE_BYTES:
                    self.fail("App-server event exceeded the worker limit.")
                if line.strip():
                    self.event(line)
            if len(self.stdout_buffer) > MAX_LINE_BYTES:
                self.fail("App-server event exceeded the worker limit.")

    def finish(self, *, require_turn=True):
        """Close input, drain output, and require an actual clean process exit."""
        until = min(self.deadline, time.monotonic() + 2)
        while self.outgoing and time.monotonic() < until:
            self.pump(allow_eof=not require_turn)
        if self.outgoing:
            self.fail("App-server did not drain its protocol replies.")
        self.process.stdin.close()
        while not (self.stdout_eof and self.stderr_eof and self.process.poll() is not None):
            if time.monotonic() >= until:
                self.fail("App-server did not stop after completing its turn.")
            self.pump(allow_eof=not require_turn)
        if self.process.returncode != 0:
            self.fail("App-server exited unsuccessfully.")

    def run(self, cwd, prompt, schema, thread_name=None):
        self.request("initialize", {"clientInfo": {"name": "job_review_browser",
                     "version": "1.0.0", "title": "Job Review Browser Worker"},
                     "capabilities": {"experimentalApi": True,
                                      "mcpServerOpenaiFormElicitation": True}})
        self.send({"method": "initialized", "params": {}})
        params = {"cwd": str(cwd), "sandbox": "read-only", "approvalPolicy": APPROVAL_POLICY,
                  "config": {"features.multi_agent": False, "features.multi_agent_v2": False},
                  "developerInstructions": "Do all Job Review work in this existing session. Do not spawn subagents or create, fork, or message other Codex chats. The host alone manages session rotation."}
        if not self.require_browser:
            # Use the same plugin restrictions as the existing browser-free
            # transport. The driver additionally rejects every non-passive item.
            from codex_transport import RANK_DISABLED_PLUGINS
            params['config'].update({f'plugins.{plugin}@openai-bundled.enabled': False
                                     for plugin in RANK_DISABLED_PLUGINS})
            params['config'].update({f'mcp_servers.{server}.enabled': False
                                     for server in ('node_repl', 'computer-use')})
            params['developerInstructions'] += (' This is a tool-free session-only stage. '
                'Use only the supplied data; do not invoke tools, browse, run commands, '
                'read files, request approvals, or start helper work.')
        original_thread = self.thread_id
        if original_thread:
            self.preload_children()
            params.update(threadId=original_thread, excludeTurns=True)
        response = self.request("thread/resume" if original_thread else "thread/start", params)
        identity = response.get("thread", {}).get("id")
        if not _identity(identity) or (self.thread_id is not None and self.thread_id != identity):
            self.fail("App-server returned a different thread identity.")
        self.thread_id = identity
        sandbox = response.get("sandbox", {})
        if (not isinstance(sandbox, dict) or sandbox.get("type") != "readOnly"
                or sandbox.get("networkAccess", False) is not False
                or response.get("approvalPolicy") != APPROVAL_POLICY):
            self.fail("App-server did not apply the restricted worker policy.")
        if thread_name:
            self.request('thread/name/set', {'threadId': identity, 'name': thread_name})
        self.check(renew=True)
        self.turn_sent = True
        response = self.request("turn/start", {"threadId": identity,
            "input": [{"type": "text", "text": prompt}], "outputSchema": schema})
        self.bind_turn(response.get("turn"))
        while self.turn_done is None or self.child_turns:
            self.pump()
        self.finish()
        self.check(renew=True)
        if self.child_turns:
            self.fail("App-server child work has no terminal notification.")
        if not self.require_browser and self.external_started:
            self.fail('Session-only worker observed external activity.')
        if self.stdout_buffer.strip():
            self.fail("App-server returned a truncated event stream.")
        self.classify_diagnostic(self.stderr_buffer)
        self.stderr_buffer = b""
        if self.turn_done == "failed" and not self.external_started and self.failures == {"capacity"}:
            self.fail("Selected model is at capacity.", capacity=True)
        if self.turn_done != "completed" or self.terminal_error:
            self.fail("App-server turn did not complete successfully.")
        if self.require_browser and not self.browser_verified:
            self.fail("App-server did not verify a browser tool call.")
        if not self.final_messages:
            self.fail("App-server returned no final result.")
        text = next(reversed(self.final_messages.values()))
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            self.fail("App-server final result exceeded the worker limit.")
        try:
            result = _loads(text)
        except (ValueError, UnicodeError, RecursionError):
            self.fail("App-server returned an invalid final result.")
        if not isinstance(result, dict):
            self.fail("App-server returned an invalid final result.")
        return AppServerResult(result, identity, True, self.browser_verified, tuple(sorted(self.tools)),
                               self.event_bytes, self.process.returncode, tuple(self.approval_events), self.turn_id)


def run_appserver(binary, cwd, prompt, schema, thread_id, timeout, heartbeat_interval,
                  heartbeat, closed, approve_request, thread_name=None, require_browser=True):
    """Run one fenced turn; never select a different model, effort, or reviewer.

    ``heartbeat`` must raise or return False when its lease is lost. ``closed``
    is a threading.Event or zero-argument predicate. ``approve_request`` returns
    an MCP form response with action/content and optionally the exact metadata
    {"persist": "session"}; unsupported data cancels. The callback must implement
    the project's explicit consent rules. Global grants and reviewer metadata
    are never synthesized or forwarded.
    """
    binary, cwd = Path(binary), Path(cwd)
    if (not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK)
            or not cwd.is_absolute() or not cwd.is_dir()
            or not isinstance(prompt, str) or not isinstance(schema, dict)
            or thread_id is not None and not _identity(thread_id)
            or type(require_browser) is not bool
            or not math.isfinite(timeout) or timeout <= 0
            or not math.isfinite(heartbeat_interval) or not 0 < heartbeat_interval < 60):
        raise ValueError("Invalid app-server worker configuration.")
    process = None
    driver = None
    try:
        stopped = closed.is_set() if hasattr(closed, "is_set") else closed()
        if stopped:
            raise AppServerError("App-server worker stopped.", safe_stopped=True, thread_id=thread_id)
        process = subprocess.Popen([str(binary), "app-server", "--listen", "stdio://"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=str(cwd), start_new_session=True, bufsize=0)
        driver = _Driver(process, thread_id=thread_id, timeout=timeout,
                         heartbeat_interval=heartbeat_interval, heartbeat=heartbeat,
                         closed=closed, approve_request=approve_request,
                         require_browser=require_browser)
        return driver.run(cwd, prompt, schema, thread_name=thread_name)
    except AppServerError:
        raise
    except Exception:
        raise AppServerError("App-server transport failed.",
                             uncertain=bool(driver and (driver.turn_sent or driver.external_started)),
                             thread_id=driver.thread_id if driver else thread_id,
                             turn_id=driver.turn_id if driver else None) from None
    finally:
        if process is not None:
            try:
                _stop(process)
            except AppServerError as error:
                error.thread_id = error.thread_id or (driver.thread_id if driver else thread_id)
                error.turn_id = error.turn_id or (driver.turn_id if driver else None)
                raise
            finally:
                if driver is not None:
                    driver.selector.close()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


def _passive_turn_proof(thread, page, thread_id, turn_id):
    """Return only safe facts; incomplete history or unknown items give no proof."""
    if (not isinstance(thread, dict) or thread.get("id") != thread_id
            or not isinstance(thread.get("status"), dict)
            or thread["status"].get("type") not in {"notLoaded", "idle"}
            or not isinstance(page, dict) or not isinstance(page.get("data"), list)
            or len(page["data"]) != 1):
        return None
    turn = page["data"][0]
    if (not isinstance(turn, dict) or turn.get("id") != turn_id
            or turn.get("status") not in {"completed", "failed", "interrupted"}
            or turn.get("itemsView") != "full" or not isinstance(turn.get("items"), list)
            or len(turn["items"]) > 256):
        return None
    start, end = turn.get("startedAt"), turn.get("completedAt")
    if type(start) is not int or type(end) is not int or not 0 < start <= end:
        return None
    kinds = {}
    identities = set()
    for item in turn["items"]:
        if (not isinstance(item, dict) or item.get("type") not in _PASSIVE_ITEMS
                or not isinstance(item.get("id"), str) or not 0 < len(item["id"]) <= 200
                or item["id"] in identities):
            return None
        identities.add(item["id"])
        kind = item["type"]
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"version": 1, "thread_id": thread_id, "turn_id": turn_id,
            "turn_status": turn["status"], "started_at": start, "completed_at": end,
            "items_view": "full", "item_count": len(turn["items"]),
            "item_type_counts": dict(sorted(kinds.items())),
            "external_item_count": 0, "child_count": 0}


def inspect_inactive_turn(binary, cwd, thread_id, turn_id, closed, timeout=15):
    """Read-only proof of an exact, latest, terminal turn with no external work.

    Reads two bounded snapshots without loading/resuming the thread. ``full``
    explicitly means every persisted item, unlike the default display summary.
    ``None`` means no proof; it is never permission to release quarantine.
    The caller owns task/receipt binding, revision fences, and recovery policy.
    No prompts, messages, arguments, or raw responses are returned or persisted.
    """
    binary, cwd = Path(binary), Path(cwd)
    if (not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK)
            or not cwd.is_absolute() or not cwd.is_dir() or not _identity(thread_id)
            or not _identity(turn_id) or not math.isfinite(timeout) or not 0 < timeout <= 60):
        raise ValueError("Invalid inactive-turn inspection configuration.")
    process, driver, proof = None, None, None
    try:
        stopped = closed.is_set() if hasattr(closed, "is_set") else closed()
        if stopped:
            return None
        process = subprocess.Popen([str(binary), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd), start_new_session=True, bufsize=0)
        driver = _Driver(process, thread_id=thread_id, timeout=timeout,
            heartbeat_interval=min(5, timeout / 2), heartbeat=lambda: True,
            closed=closed, approve_request=lambda request: {"action": "cancel", "content": None})
        driver.request("initialize", {"clientInfo": {"name": "job_review_inactive_turn_check",
            "version": "1.0.0"}, "capabilities": {"experimentalApi": True}})
        driver.send({"method": "initialized", "params": {}})
        snapshots = []
        for _ in range(2):
            thread = driver.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            page = driver.request("thread/turns/list", {"threadId": thread_id, "limit": 1,
                                                       "sortDirection": "desc", "itemsView": "full"})
            snapshot = _passive_turn_proof(thread.get("thread"), page, thread_id, turn_id)
            if snapshot is None:
                break
            snapshots.append(snapshot)
        driver.finish(require_turn=False)
        driver.check()
        driver.classify_diagnostic(driver.stderr_buffer)
        if (len(snapshots) == 2 and snapshots[0] == snapshots[1]
                and not driver.stdout_buffer.strip() and not driver.failures
                and not driver.request_ids and not driver.turn_observed and not driver.external_started):
            proof = dict(snapshots[0], observed_at=datetime.now(timezone.utc).isoformat(),
                         stable_snapshots=2, inspector_exit_code=process.returncode)
    except Exception as error:
        # The documented result is no proof on transport/validation failure.
        # Never substitute notLoaded, process exit, or a partial page for proof.
        LOGGER.warning("Inactive-turn inspection failed (%s).", type(error).__name__)
        proof = None
    finally:
        if process is not None:
            try:
                _stop(process)
            except Exception as error:
                LOGGER.warning("Inactive-turn inspection cleanup failed (%s).", type(error).__name__)
                proof = None
            finally:
                if driver is not None:
                    driver.selector.close()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
    return proof


def _completed_worker_turn(thread, page, identity, receipt, expected_result, receipt_time):
    """Bind a terminal native turn to the host's private completion receipt."""
    if (not isinstance(thread, dict) or thread.get('id') != identity
            or not isinstance(thread.get('status'), dict)
            or thread['status'].get('type') not in {'notLoaded', 'idle'}
            or not isinstance(page, dict) or not isinstance(page.get('data'), list)
            or len(page['data']) != 1):
        raise ValueError('Worker conversation has no verified terminal turn.')
    turn = page['data'][0]
    if (not isinstance(turn, dict) or turn.get('status') != 'completed'
            or not _identity(turn.get('id')) or turn.get('itemsView') != 'full'
            or not isinstance(turn.get('items'), list)
            or type(turn.get('completedAt')) is not int or turn['completedAt'] > receipt_time + 5
            or (receipt.get('turn_id') and turn['id'] != receipt['turn_id'])):
        raise ValueError('Worker conversation changed after its completed receipt.')
    messages = [item for item in turn['items'] if item.get('type') == 'agentMessage']
    if not messages or _loads(messages[-1].get('text', '')) != expected_result:
        raise ValueError('Worker conversation result differs from its receipt.')
    return turn


def archive_worker_thread(binary, cwd, thread_id, *, receipt, run_directory,
                          receipt_name, closed, archive=True, timeout=30):
    """Verify and optionally archive one host-owned worker without starting a turn.

    A complete host receipt and the exact latest native result must agree. All
    spawned descendants must be terminal and older than that completed turn.
    Failed, unknown, changed, or active work is left untouched.
    """
    from codex_transport import _private_json
    binary, cwd, run_directory = Path(binary), Path(cwd), Path(run_directory)
    if (not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK)
            or not cwd.is_absolute() or not cwd.is_dir() or not _identity(thread_id)
            or not isinstance(receipt_name, str)
            or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}-[0-9a-f-]{36}', receipt_name)
            or not math.isfinite(timeout) or not 0 < timeout <= 60):
        raise ValueError('Invalid worker archive configuration.')
    run = run_directory / receipt_name
    if run.is_symlink() or _private_json(run / 'receipt.json') != receipt:
        raise ValueError('Worker archive receipt changed.')
    if (receipt.get('thread_id') != thread_id or receipt.get('transport') != 'app-server'
            or receipt.get('status') != 'completed' or receipt.get('browser') is not True
            or receipt.get('browser_verified') is not True or receipt.get('turn_completed') is not True
            or type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0):
        raise ValueError('Worker archive requires a complete private receipt.')
    expected = _private_json(run / 'result.json', max_bytes=MAX_RESULT_BYTES)
    receipt_time = (run / 'receipt.json').stat().st_mtime
    process, driver = None, None
    try:
        process = subprocess.Popen([str(binary), 'app-server', '--listen', 'stdio://'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd), start_new_session=True, bufsize=0)
        driver = _Driver(process, thread_id=thread_id, timeout=timeout, heartbeat_interval=5,
            heartbeat=lambda: True, closed=closed,
            approve_request=lambda request: {'action': 'cancel', 'content': None})
        driver.request('initialize', {'clientInfo': {'name': 'job_review_session_lifecycle',
            'version': '1.0.0'}, 'capabilities': {'experimentalApi': True}})
        driver.send({'method': 'initialized', 'params': {}})
        previous = None
        for _ in range(2):
            metadata = driver.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
            page = driver.request('thread/turns/list', {'threadId': thread_id, 'limit': 1,
                'sortDirection': 'desc', 'itemsView': 'full'})
            turn = _completed_worker_turn(metadata.get('thread'), page, thread_id, receipt, expected, receipt_time)
            if previous is not None and turn != previous:
                raise ValueError('Worker conversation changed during inspection.')
            previous = turn
        driver.preload_children()
        for child in driver.child_threads:
            page = driver.request('thread/turns/list', {'threadId': child, 'limit': 1,
                'sortDirection': 'desc', 'itemsView': 'full'})
            turns = page.get('data')
            if not isinstance(turns, list) or len(turns) != 1:
                raise ValueError('Worker child has no terminal receipt.')
            last = turns[0]
            if (last.get('status') not in {'completed', 'failed', 'interrupted'}
                    or type(last.get('completedAt')) is not int
                    or last['completedAt'] > previous['completedAt']):
                raise ValueError('Worker child changed after the parent completed.')
        if archive:
            driver.request('thread/archive', {'threadId': thread_id})
        driver.finish(require_turn=False)
        driver.check()
        driver.classify_diagnostic(driver.stderr_buffer)
        if driver.stdout_buffer.strip() or driver.failures or driver.request_ids or driver.external_started:
            raise ValueError('Worker lifecycle check was not passive.')
        return {'thread_id': thread_id, 'turn_id': previous['id'], 'archived': archive,
                'child_count': len(driver.child_threads), 'verified_at': datetime.now(timezone.utc).isoformat()}
    finally:
        if process is not None:
            try:
                _stop(process)
            finally:
                if driver is not None:
                    driver.selector.close()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
