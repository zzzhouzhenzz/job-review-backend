#!/usr/bin/env python3
"""Run the authenticated review backend and TLS gateway as one foreground service.

``check`` validates private configuration, certificates and Caddy without opening
the database. ``run`` also locks and backs up the database, then starts both
services. ``status`` probes a running stack over local HTTPS. It never replaces
another process or enables the agent adapters.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import fcntl
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import threading
import time

from server import ReviewServer
import tls_gateway


@dataclass(frozen=True)
class StackConfig:
    database: Path
    gateway: tls_gateway.GatewayConfig


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def load_config(path):
    """Read one owner-owned 0600 JSON file; relative paths use its directory."""
    path = Path(path).expanduser().absolute()
    try:
        data = json.loads(tls_gateway._private_read(path, 16 * 1024), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Stack configuration must be a valid JSON object") from None
    if (not isinstance(data, dict) or set(data) != {"schema_version", "database", "gateway"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise ValueError("Stack configuration requires schema_version 1, database and gateway")

    def local_path(value, name):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{name} must be a nonempty file path")
        result = Path(value).expanduser()
        # Keep the final path component intact so the gateway's O_NOFOLLOW
        # checks still reject symbolic links to credentials and private keys.
        return Path(os.path.abspath(result if result.is_absolute() else path.parent / result))

    database = local_path(data["database"], "database").resolve()
    gateway = data["gateway"]
    allowed = {field.name for field in fields(tls_gateway.GatewayConfig)}
    required = {"access_config", "credential_file", "cert_file", "key_file"}
    if not isinstance(gateway, dict) or not required <= set(gateway) or set(gateway) - allowed:
        raise ValueError("gateway must contain the required GatewayConfig fields and no unknown fields")
    values = dict(gateway)
    for name in required | {"ca_file"}:
        if name in values and (name != "ca_file" or values[name] is not None):
            values[name] = local_path(values[name], name)
    for name in ("backend_port", "listen_port"):
        if name in values and not (name == "listen_port" and values[name] is None):
            if type(values[name]) is not int or not 1 <= values[name] <= 65535:
                raise ValueError(f"{name} must be an integer between 1 and 65535")
    if "listen_address" in values and not isinstance(values["listen_address"], str):
        raise ValueError("listen_address must be an IPv4 address string")
    if "caddy" in values:
        if not isinstance(values["caddy"], str) or not values["caddy"].strip():
            raise ValueError("caddy must be an executable name or path")
        if "/" in values["caddy"] or values["caddy"].startswith("~"):
            values["caddy"] = str(local_path(values["caddy"], "caddy"))
    return StackConfig(database, tls_gateway.GatewayConfig(**values))


def check(config):
    """Validate local TLS inputs and Caddy; no listeners or database access."""
    prepared = tls_gateway.prepare(config.gateway)
    with tls_gateway._caddy_environment() as environment:
        tls_gateway._validate(prepared, environment)
    return prepared


def _local_tls_connection(prepared):
    address = prepared.config.listen_address
    if address == "0.0.0.0":
        address = "127.0.0.1"
    client = http.client.HTTPConnection(address, prepared.listen_port, timeout=3)
    raw = socket.create_connection((address, prepared.listen_port), timeout=3)
    try:
        client.sock = prepared.client_context.wrap_socket(raw, server_hostname=prepared.hostname)
    except BaseException:
        raw.close()
        raise
    return client


def status(config):
    """Read current authenticated health over TLS; does not test a remote route."""
    prepared = tls_gateway.prepare(config.gateway)
    for authenticated in (False, True):
        tls_gateway._read_probe(_local_tls_connection(prepared), prepared, authenticated=authenticated)
    client = _local_tls_connection(prepared)
    try:
        client.request("GET", "/api/v1/health", headers={"Host": prepared.access.public_host,
            "Authorization": "Bearer " + prepared.credential, "Connection": "close"})
        response = client.getresponse()
        body = response.read(tls_gateway.MAX_PROBE_BYTES + 1)
        if response.status != 200:
            raise ValueError(f"Authenticated health probe returned HTTP {response.status}")
        if len(body) > tls_gateway.MAX_PROBE_BYTES:
            raise ValueError("Health response exceeds the size limit")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Health response is not valid JSON") from None
        health = payload.get("health") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                or not isinstance(health, dict) or not isinstance(health.get("status"), str)
                or type(health.get("ready")) is not bool):
            raise ValueError("Health response does not contain a valid v1 readiness report")
        return {"public_origin": prepared.access.public_origin, "local_api_reachable": True,
                "worker_status": health["status"], "worker_ready": health["ready"]}
    finally:
        client.close()


@contextmanager
def database_lock(database):
    """Cooperating stack processes share one lock for the canonical DB path."""
    database = Path(database).resolve()
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = database.with_name(database.name + ".device-stack.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("Database lock must be an owner-owned regular file with mode 0600")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another device stack holds this database lock") from None
        yield
    finally:
        os.close(descriptor)


def backup_database(database):
    """Copy a consistent SQLite snapshot, including committed WAL contents."""
    database = Path(database).resolve()
    if not database.exists():
        return None
    if not database.is_file():
        raise ValueError("Database path must identify a regular file")
    directory = database.parent / "backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Backup directory must be owner-owned with mode 0700, without a symlink")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = directory / f"{database.name}.{timestamp}.{secrets.token_hex(4)}.backup.sqlite3"
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError("Database backup did not complete within 30 seconds")

    try:
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            destination = sqlite3.connect(backup, timeout=3)
            try:
                source.backup(destination, pages=128, progress=progress, sleep=0.05)
                if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("Database backup failed SQLite quick_check")
            finally:
                destination.close()
        finally:
            source.close()
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


@dataclass
class RunningStack:
    server: ReviewServer
    thread: threading.Thread
    gateway: object
    backup: Path | None
    failures: list

    def check_running(self):
        if self.failures:
            raise RuntimeError("Backend serving thread failed") from self.failures[0]
        if not self.thread.is_alive():
            raise RuntimeError("Backend serving thread stopped unexpectedly")
        status = self.gateway.poll()
        if status is not None:
            raise RuntimeError(f"TLS gateway stopped unexpectedly with status {status}")


def _stop_backend(server, thread):
    # shutdown waits for serve_forever, so never invoke it on that same thread.
    if thread.is_alive():
        stopper = threading.Thread(target=server.shutdown, name="review-shutdown", daemon=True)
        stopper.start()
        stopper.join(5)
    server.server_close()
    thread.join(5)
    if thread.is_alive():
        raise RuntimeError("Backend did not stop within the shutdown deadline")


@contextmanager
def running_stack(config, *, log_output=None):
    """Own both services until exit; each startup failure unwinds its resources."""
    check(config)
    with database_lock(config.database):
        # Refuse an occupied backend port before even creating a backup. The
        # ReviewServer constructor also binds before database initialization.
        with socket.socket() as reservation:
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reservation.bind(("127.0.0.1", config.gateway.backend_port))
            backup = backup_database(config.database)
        server = ReviewServer(("127.0.0.1", config.gateway.backend_port), config.database,
                              access_config=config.gateway.access_config)
        failures = []

        def serve():
            try:
                server.serve_forever(poll_interval=0.1)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=serve, name="review-backend", daemon=True)
        try:
            thread.start()
            with tls_gateway.running_gateway(config.gateway, log_output=log_output) as gateway:
                stack = RunningStack(server, thread, gateway, backup, failures)
                stack.check_running()
                yield stack
        finally:
            if thread.ident is not None:
                _stop_backend(server, thread)
            else:
                server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run", "status"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)

    def stop(signum, frame):
        raise KeyboardInterrupt

    # This is a foreground CLI process. Do not alter umask in library helpers
    # that may run alongside other application threads.
    previous_mask = os.umask(0o077)
    previous_signal = signal.signal(signal.SIGTERM, stop)
    try:
        config = load_config(args.config)
        if args.command == "check":
            prepared = check(config)
            print(f"Local stack configuration passed for {prepared.access.public_origin}. "
                  "No database or listener was opened.")
            return
        if args.command == "status":
            report = status(config)
            print(f"Local authenticated HTTPS API is reachable at {report['public_origin']}.")
            print(f"Worker readiness: {report['worker_status']} (ready={report['worker_ready']}).")
            print("This local probe does not verify Internet or iPhone connectivity.")
            return
        with running_stack(config) as stack:
            if stack.backup is not None:
                print(f"Database backup verified: {stack.backup}", flush=True)
            print("API and TLS ready. Certificate and authenticated HTTPS probe passed.", flush=True)
            health = stack.server.orchestrator.readiness.snapshot()
            print(f"Worker readiness: {health['status']}; agent adapters remain unconfigured.", flush=True)
            while True:
                stack.check_running()
                time.sleep(0.25)
    except KeyboardInterrupt:
        print("Device stack stopped.", flush=True)
    except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError,
            http.client.HTTPException) as error:
        parser.exit(1, f"Device stack error: {error}\n")
    finally:
        signal.signal(signal.SIGTERM, previous_signal)
        os.umask(previous_mask)


if __name__ == "__main__":
    main()
