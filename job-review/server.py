#!/usr/bin/env python3
"""Serve the continuous local job review with optional explicit worker runtime."""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from device_access import DeviceAccess
from orchestrator import Orchestrator
from store import ReviewError, digest
from workflow import WorkflowStore, capabilities

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "state" / "review.sqlite3"
MAX_BODY = 1_000_000
LOGGER = logging.getLogger(__name__)
PUBLIC_TASK_FIELDS = ("id", "kind", "status", "attempts", "failure_count", "max_attempts",
                      "available_at", "resource", "resource_quarantined", "error", "created_at", "updated_at")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path=DEFAULT_DB, root=ROOT, *, runtime=None, readiness_options=None,
                 access_config=None, run_workers=False, run_scheduling=True):
        if address[0] != "127.0.0.1":
            raise ValueError("The review server must bind to 127.0.0.1.")
        # Validate access configuration before opening a database or a listener.
        self.access = DeviceAccess.from_file(access_config) if access_config is not None else None
        # Reserve the listener before migrations. An occupied port must not
        # change a database for a server which cannot start.
        super().__init__(address, Handler)
        try:
            self.store = WorkflowStore(db_path)
            self.run_scheduling=run_scheduling
            self.orchestrator = Orchestrator(self.store, runtime, readiness_options=readiness_options,
                                             schedule_answer_reviews=run_workers)
            self._last_tick = time.monotonic()
            self.root = Path(root)
            self.token = secrets.token_urlsafe(32)
            if run_scheduling:
                self.orchestrator.readiness.start()
            self.worker_runner = None
            if run_workers and self.orchestrator.runtime.enabled:
                from worker_runner import WorkerRunner
                self.worker_runner = WorkerRunner(self.orchestrator)
                self.worker_runner.start()
        except BaseException:
            self.server_close()
            raise

    def worker_capabilities(self):
        return {**capabilities(), "worker_enabled": bool(self.orchestrator.runtime.enabled),
                "worker_kinds": list(getattr(self.orchestrator.runtime, "supported_kinds", [])),
                "worker_status": self.orchestrator.readiness.snapshot()["status"],
                "remote_access": self.access is not None, "application_workflow": True,
                "question_sync": getattr(self.store, "question_pool", None) is not None}

    def server_close(self):
        # HTTPServer also calls this if binding fails during construction.
        if getattr(self, "worker_runner", None) is not None:
            # Runtime cancellation precedes the join. Do not release a supervisor
            # database lock while a worker can still commit or use Chrome.
            self.worker_runner.stop(timeout=None)
        if hasattr(self, "orchestrator"):
            self.orchestrator.readiness.close()
        super().server_close()

    def service_actions(self):
        """Keep scheduling independent of long-running Codex calls."""
        if not self.run_scheduling:
            return
        self.orchestrator.readiness.poll()
        current = time.monotonic()
        if current - self._last_tick < 5:
            return
        self._last_tick = current
        try:
            self.orchestrator.tick()
        except Exception:
            LOGGER.exception("job-review: Orchestrator scheduling tick failed")


class Handler(BaseHTTPRequestHandler):
    def send_error(self, code, message=None, explain=None):
        if self.server.access is not None:
            # The base parser includes malformed request text in its error log
            # and HTML response, before do_GET/do_POST can authenticate it.
            return self.error(code, "The HTTP request could not be processed.", v1=True)
        return super().send_error(code, message, explain)

    def log_request(self, code="-", size="-"):
        if self.server.access is not None:
            # Do not record query strings or paths supplied by a remote caller.
            print(f"job-review: authenticated API response {code}", flush=True)
        else:
            super().log_request(code, size)

    def log_message(self, format, *args):
        print(f"job-review: {format % args}", flush=True)

    def send(self, status, body, content_type="application/json; charset=utf-8", *, download=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="jobswipe"')
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="job-review-export.json"')
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.server.access is not None:
            allowed.add(self.server.access.public_host)
        values = self.headers.get_all("Host", [])
        return len(values) == 1 and values[0] in allowed

    def authenticated(self):
        # A proxy reaches us over loopback too. Neither peer address nor Host
        # nor any Forwarded/identity header can bypass this credential check.
        return self.server.access is None or self.server.access.authenticate(self.headers)

    def error(self, status, message, *, v1=False, uncertain=False, reason=None):
        if not v1:
            return self.send(status, {"error": message})
        code = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict"}.get(status, "internal_error")
        return self.send(status, {
            "schema_version": 1,
            "error": {"code": code, "message": message,
                      "reason": (reason or "operation_rejected") if status == 409 else None,
                      "commit_status": "unknown" if uncertain else "not_committed"},
        })

    def v1_read(self, data):
        return self.send(200, {
            "schema_version": 1, "revision": digest(data), "complete": True,
            "next_cursor": None, "token": self.server.token, **data,
        })

    def do_GET(self):
        path = urlsplit(self.path).path
        v1 = path.startswith("/api/v1/")
        if (path == "/api/v1/project-consent" and self.server.access is None) or not self.authenticated():
            return self.error(401, "A valid device credential is required.", v1=v1)
        if not self.valid_host():
            return self.error(403, "Invalid host.", v1=v1)
        try:
            if path == "/api/v1/capabilities":
                return self.v1_read({"capabilities": self.server.worker_capabilities()})
            if path == "/api/v1/question-sync":
                from question_sync import snapshot
                return self.v1_read({"question_sync": snapshot(self.server.store)})
            if path == "/api/v1/application-workflow":
                return self.v1_read({"application_workflow": self.server.store.application_workflow()})
            if path == "/api/v1/health":
                return self.v1_read({"health": self.server.orchestrator.readiness.snapshot()})
            if path == "/api/v1/project-consent":
                return self.v1_read({"project_consent": self.server.store.project_consent()})
            if path == "/api/v1/feed":
                feed = self.server.store.feed()
                feed["capabilities"] = self.server.worker_capabilities()
                return self.v1_read(feed)
            if path == "/api/v1/processes":
                pool=getattr(self.server.store,'question_pool',None)
                if pool is not None:
                    with pool.hold('recovery','processes-read',timeout_seconds=1):
                        from pool_application import recover_locked
                        recover_locked(self.server.store)
                        processes=self.server.store.processes()
                else:
                    processes=self.server.store.processes()
                return self.v1_read({"processes": processes})
            if path == "/api/v1/questions":
                pool=getattr(self.server.store,'question_pool',None)
                if pool is not None:
                    with pool.hold('recovery','questions-read',timeout_seconds=1):
                        from pool_application import recover_locked
                        recover_locked(self.server.store)
                        questions=self.server.store.questions()
                else:
                    questions=self.server.store.questions()
                return self.v1_read({"questions": questions})
            if path == "/api/v1/unblock-instructions":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) != {"task_id"} or len(query['task_id']) != 1:
                    raise ReviewError('Provide exactly one task_id query parameter.')
                return self.v1_read(self.server.store.unblock_instructions(query['task_id'][0]))
            if path == "/api/v1/tasks":
                return self.v1_read({"tasks": [{key: task.get(key) for key in PUBLIC_TASK_FIELDS}
                                               for task in self.server.store.tasks()]})
            if path in {"/api/state", "/api/export"}:
                state = self.server.store.state()
                if path == "/api/state":
                    state["token"] = self.server.token
                return self.send(200, state, download=path == "/api/export")
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/index.html": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in files:
                name, mime = files[path]
                return self.send(200, (self.server.root / name).read_bytes(), mime)
            return self.error(404, "Page not found.", v1=v1)
        except ReviewError as error:
            return self.error(error.status, str(error), v1=v1, reason=error.reason)
        except (OSError, sqlite3.Error) as error:
            self.log_error("Read failed: %s", error)
            return self.error(500, "The saved review could not be read. Check the server log.", v1=v1, uncertain=True)
        except Exception:
            if not v1:
                raise
            LOGGER.exception("job-review: Versioned read failed for %s", path)
            return self.error(500, "The saved workflow could not be read. Check the server log.", v1=True, uncertain=True)

    def do_POST(self):
        path = urlsplit(self.path).path
        v1 = path.startswith("/api/v1/")
        if (path == "/api/v1/project-consent" and self.server.access is None) or not self.authenticated():
            return self.error(401, "A valid device credential is required.", v1=v1)
        if not self.valid_host():
            return self.error(403, "Invalid host.", v1=v1)
        origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if self.server.access is not None:
            origins = {self.server.access.public_origin}
        origin_values = self.headers.get_all("Origin", [])
        if len(origin_values) != 1 or origin_values[0] not in origins:
            return self.error(403, "Use the configured server origin when saving choices.", v1=v1)
        token_values = self.headers.get_all("X-Review-Token", [])
        token = token_values[0] if len(token_values) == 1 else ""
        if not secrets.compare_digest(token.encode("utf-8"), self.server.token.encode("ascii")):
            return self.error(403, "Refresh the page before saving.", v1=v1)
        if v1:
            return self.v1_command(path)
        routes = {"/api/job": self.server.store.edit_job,
                  "/api/company": self.server.store.edit_company,
                  "/api/research": self.server.store.request_research,
                  "/api/card": self.server.store.review_card,
                  "/api/queue": self.server.store.queue_jobs,
                  "/api/cancel": self.server.store.cancel}
        operation = routes.get(path)
        if operation is None:
            return self.send(404, {"error": "Action not found."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ReviewError("The request is empty or too large.")
            if self.headers.get_content_type() != "application/json":
                raise ReviewError("Send choices as application/json.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ReviewError("The request must be a JSON object.")
            state = operation(payload)
            return self.send(200, {**state, "token": self.server.token})
        except ReviewError as error:
            return self.send(error.status, {"error": str(error)})
        except (ValueError, TypeError, UnicodeDecodeError):
            return self.send(400, {"error": "The request contains invalid JSON or fields."})
        except sqlite3.Error as error:
            self.log_error("Save failed: %s", error)
            return self.send(500, {"error": "The choice could not be saved. Refresh before trying again."})

    def v1_command(self, path):
        routes = {"/api/v1/decisions": self.server.store.receive_decision,
                  "/api/v1/answers": self.server.store.answer_questions,
                  "/api/v1/unblock-instructions": self.server.store.save_unblock_instruction,
                  "/api/v1/project-consent": self.server.store.grant_project_consent,
                  "/api/v1/tasks/cancel": self.server.store.cancel_task_command}
        operation = routes.get(path)
        if operation is None:
            return self.error(404, "Action not found.", v1=True)
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                raise ReviewError("Send one valid Content-Length header.")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise ReviewError("The request is empty or too large.")
            if self.headers.get("Transfer-Encoding") is not None:
                raise ReviewError("Transfer-Encoding is not supported.")
            if self.headers.get_content_type() != "application/json":
                raise ReviewError("Send choices as application/json.")

            def invalid_constant(value):
                raise ValueError(f"Invalid JSON constant: {value}")

            payload = json.loads(self.rfile.read(length), parse_constant=invalid_constant)
            if not isinstance(payload, dict):
                raise ReviewError("The request must be a JSON object.")
        except ReviewError as error:
            return self.error(error.status, str(error), v1=True, reason=error.reason)
        except (ValueError, TypeError, UnicodeDecodeError):
            return self.error(400, "The request contains invalid JSON or fields.", v1=True)

        # A command returns its saved acknowledgement. A second read could fail
        # after commit and must not make a saved decision look rejected.
        try:
            acknowledgement = operation(payload)
            result = {"schema_version": 1, "token": self.server.token, **acknowledgement}
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        except ReviewError as error:
            return self.error(error.status, str(error), v1=True, reason=error.reason)
        except Exception:
            LOGGER.exception("job-review: Versioned command failed for %s", path)
            return self.error(500, "The command outcome is uncertain. Retry the same request unchanged.",
                              v1=True, uncertain=True)
        return self.send(200, encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--access-config", type=Path,
                        help="Private device access configuration. Requires bearer authentication for all requests; use behind HTTPS.")
    parser.add_argument("--runtime-config", type=Path,
                        help="Explicit Codex worker configuration. Omit to keep execution disabled.")
    args = parser.parse_args()
    runtime = None
    if args.runtime_config is not None:
        from codex_runtime import load_runtime
        runtime = load_runtime(args.runtime_config)
    server = ReviewServer(("127.0.0.1", args.port), db_path=args.db, access_config=args.access_config,
                          runtime=runtime, run_workers=runtime is not None)
    print(f"Job review: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"State: {args.db.resolve()}", flush=True)
    if server.access is not None:
        print(f"Authenticated API origin: {server.access.public_origin} (HTTPS gateway required)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
