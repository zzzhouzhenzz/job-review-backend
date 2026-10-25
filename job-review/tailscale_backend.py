#!/usr/bin/env python3
"""Supervise the authenticated loopback API behind externally managed Tailscale Serve.

This command does not install or configure Tailscale, expose a network listener,
or enable agents unless --runtime-config is supplied. Serve configuration may outlive this process.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import Message
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import threading
import time

from device_access import DeviceAccess
from device_stack import backup_database, database_lock
from server import ReviewServer
from tls_gateway import _private_read, MAX_PROBE_BYTES


@dataclass(frozen=True)
class BackendConfig:
    database: Path
    access_config: Path
    credential_file: Path
    port: int = 8766
    runtime_config: Path | None = None
    no_workers: bool = False


@dataclass(frozen=True)
class PreparedBackend:
    access: DeviceAccess
    credential: str = field(repr=False)


def check(config):
    """Validate authentication inputs without opening the database or a listener."""
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError("Backend port must be an integer between 1 and 65535")
    if type(config.no_workers) is not bool:
        raise ValueError('No-workers must be an explicit boolean')
    if config.access_config is None or config.credential_file is None:
        raise ValueError("Private device access configuration and credential file are required")
    access = DeviceAccess.from_file(config.access_config)
    try:
        credential = _private_read(config.credential_file, 128).decode("ascii").removesuffix("\n")
    except UnicodeError:
        raise ValueError("Credential file must contain the provisioned ASCII token") from None
    headers = Message()
    headers["Authorization"] = "Bearer " + credential
    if not access.authenticate(headers):
        raise ValueError("Credential file does not match the device access configuration")
    return PreparedBackend(access, credential)


class TailnetReviewServer(ReviewServer):
    """Bound socket reads and concurrent requests without relying on Caddy."""

    # Join request threads on close, after closing their sockets. Do not release
    # the database lock while an accepted request is still saving a decision.
    daemon_threads = False
    request_timeout = 10
    max_connections = 16

    def __init__(self, *args, no_workers=False, **kwargs):
        self.no_workers=no_workers
        if no_workers:
            kwargs.update(run_workers=False,run_scheduling=False)
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._requests = set()
        self._requests_lock = threading.Lock()
        self._closing = False
        super().__init__(*args, **kwargs)

    def worker_capabilities(self):
        value=super().worker_capabilities()
        if self.no_workers:
            value.update(worker_enabled=False,worker_status='paused')
        return value

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._requests_lock:
            self._requests.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_request(request)
            raise

    def _release_request(self, request):
        with self._requests_lock:
            self._requests.discard(request)
        self._slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_request(request)

    def server_close(self):
        self._closing = True
        with self._requests_lock:
            for request in self._requests:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # A completed request may already have closed its socket.
                    pass
                self.shutdown_request(request)
        super().server_close()
        if self.no_workers and hasattr(self,'orchestrator'):
            close=getattr(self.orchestrator.runtime,'close',None)
            if callable(close):close()

    def handle_error(self, request, client_address):
        # Closing a partial request can make its handler reach EOF and then
        # attempt a response. This expected shutdown error is not an API fault.
        if self._closing and isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _probe(config, prepared):
    """Verify local 401/200 responses; no claim about TLS or phone reachability."""
    report = None
    for authenticated in (False, True):
        client = http.client.HTTPConnection("127.0.0.1", config.port, timeout=3)
        headers = {"Host": prepared.access.public_host, "Connection": "close"}
        if authenticated:
            headers["Authorization"] = "Bearer " + prepared.credential
        try:
            client.request("GET", "/api/v1/capabilities", headers=headers)
            response = client.getresponse()
            body = response.read(MAX_PROBE_BYTES + 1)
            if len(body) > MAX_PROBE_BYTES:
                raise ValueError("Backend capabilities response exceeds the size limit")
            expected = 200 if authenticated else 401
            if response.status != expected:
                raise ValueError(f"Local authentication probe expected HTTP {expected}, received {response.status}")
            if not authenticated:
                continue
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError("Backend capabilities response is not valid JSON") from None
            capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
                    or payload["schema_version"] != 1 or not isinstance(capabilities, dict)
                    or capabilities.get("remote_access") is not True
                    or type(capabilities.get("worker_enabled")) is not bool
                    or not isinstance(capabilities.get("worker_status"), str)):
                raise ValueError("Backend must report authenticated access and a valid worker status")
            report = {"public_origin": prepared.access.public_origin, "local_api_reachable": True,
                      "worker_status": capabilities["worker_status"], "worker_enabled": capabilities["worker_enabled"]}
        finally:
            client.close()
    return report


def status(config):
    return _probe(config, check(config))


@dataclass
class RunningBackend:
    server: TailnetReviewServer
    thread: threading.Thread
    backup: Path | None
    failures: list

    def check_running(self):
        if self.failures:
            raise RuntimeError("Backend serving thread failed") from self.failures[0]
        if not self.thread.is_alive():
            raise RuntimeError("Backend serving thread stopped unexpectedly")
        if self.server.worker_runner is not None:
            self.server.worker_runner.check_running()


def _stop_backend(server, thread):
    # A scheduling tick can outlive a socket timeout. Keep the database lock
    # until it returns; a timed join would let a second process migrate or
    # change the database while the old serving thread still uses it.
    if thread.is_alive():
        server.shutdown()
    server.server_close()
    thread.join()


@contextmanager
def running_backend(config):
    prepared = check(config)
    runtime = None
    if config.runtime_config is not None:
        from codex_runtime import load_runtime
        runtime = load_runtime(config.runtime_config)
    with database_lock(config.database):
        # Keep the port reserved during the backup. ReviewServer also binds
        # before migration, so a subsequent port race cannot mutate the DB.
        with socket.socket() as reservation:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reservation.bind(("127.0.0.1", config.port))
            backup = backup_database(config.database)
        server = TailnetReviewServer(("127.0.0.1", config.port), config.database,
                                    access_config=config.access_config, runtime=runtime,
                                    run_workers=runtime is not None,no_workers=config.no_workers)
        failures = []

        def serve():
            try:
                server.serve_forever(poll_interval=0.1)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=serve, name="review-tailnet-backend", daemon=True)
        try:
            thread.start()
            backend = RunningBackend(server, thread, backup, failures)
            _probe(config, prepared)
            backend.check_running()
            yield backend
        finally:
            if thread.ident is not None:
                _stop_backend(server, thread)
            else:
                server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run", "status"))
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--access-config", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--runtime-config", type=Path,
                        help="Enable the configured local Codex worker lanes.")
    parser.add_argument('--no-workers',action='store_true',
                        help='Load the runtime and shared question API without worker threads or background scheduling.')
    args = parser.parse_args(argv)
    config = BackendConfig(args.db.expanduser(), args.access_config.expanduser(),
                           args.credential_file.expanduser(), args.port, args.runtime_config,args.no_workers)

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous_mask = os.umask(0o077)
    previous_signals = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        if args.command == "check":
            prepared = check(config)
            print(f"Private authentication inputs passed for {prepared.access.public_origin}.")
            print("No database, listener, or Tailscale configuration was changed.")
            return
        if args.command == "status":
            report = status(config)
            print(f"Local authenticated API is reachable; worker status: {report['worker_status']}.")
            print("This HTTP probe does not verify Tailscale HTTPS or iPhone connectivity.")
            return
        with running_backend(config) as backend:
            if backend.backup is not None:
                print(f"Database backup verified: {backend.backup}", flush=True)
            print(f"Authenticated backend ready on 127.0.0.1:{config.port}; workers configured={backend.server.orchestrator.runtime.enabled}; workers paused={config.no_workers}.", flush=True)
            print("Tailscale Serve is managed separately and may remain configured after shutdown.", flush=True)
            while True:
                backend.check_running()
                time.sleep(0.25)
    except KeyboardInterrupt:
        print("Tailscale backend stopped. External Serve configuration was not changed.", flush=True)
    except (ValueError, OSError, RuntimeError, sqlite3.Error, http.client.HTTPException) as error:
        parser.exit(1, f"Tailscale backend error: {error}\n")
    finally:
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)
        os.umask(previous_mask)


if __name__ == "__main__":
    main()
