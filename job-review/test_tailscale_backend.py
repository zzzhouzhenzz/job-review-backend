"""Fictional local tests for the backend supervised behind Tailscale Serve."""
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import tailscale_backend
from device_stack import database_lock
from tailscale_backend import BackendConfig, check, running_backend, status


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class TailscaleBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "state" / "fictional.sqlite3"
        self.access = self.root / "access.json"
        self.credential = self.root / "credential.txt"
        self.token = secrets.token_urlsafe(32)
        self.origin = "https://fictional-host.example.ts.net"
        self.access.write_text(json.dumps({"schema_version": 1, "public_origin": self.origin,
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest()}))
        self.credential.write_text(self.token + "\n")
        self.access.chmod(0o600)
        self.credential.chmod(0o600)
        self.port = unused_port()
        self.config = BackendConfig(self.database, self.access, self.credential, self.port)

    def request(self, method="GET", path="/api/v1/capabilities", body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def assert_released(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
        with database_lock(self.database):
            pass

    def seed_approval(self):
        self.database.parent.mkdir()
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE saved_approval (job TEXT, decision TEXT)")
        connection.execute("INSERT INTO saved_approval VALUES ('fictional:42', 'approved')")
        connection.commit()
        return connection

    def test_check_validates_private_credentials_without_database_or_listener(self):
        prepared = check(self.config)
        self.assertEqual(self.origin, prepared.access.public_origin)
        self.assertNotIn(self.token, repr(prepared))
        self.assertFalse(self.database.parent.exists())
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.port))
        self.credential.write_text(secrets.token_urlsafe(32))
        with self.assertRaisesRegex(ValueError, "does not match"):
            with running_backend(self.config):
                self.fail("Mismatched credential started backend")
        self.assertFalse(self.database.parent.exists())

    def test_missing_auth_or_insecure_secret_is_rejected_before_database_access(self):
        with self.assertRaisesRegex(ValueError, "required"):
            check(BackendConfig(self.database, None, None, self.port))
        for secret in (self.access, self.credential):
            with self.subTest(secret=secret.name):
                secret.chmod(0o644)
                with self.assertRaisesRegex(ValueError, "0600"):
                    with running_backend(self.config):
                        self.fail("Insecure credential started backend")
                secret.chmod(0o600)
        link = self.root / "credential-link"
        link.symlink_to(self.credential)
        with self.assertRaises(OSError):
            check(BackendConfig(self.database, self.access, link, self.port))
        self.assertFalse(self.database.parent.exists())

    def test_occupied_port_does_not_back_up_or_migrate_database(self):
        self.database.parent.mkdir()
        self.database.write_bytes(b"must not open or migrate")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.port))
            listener.listen()
            with self.assertRaises(OSError):
                with running_backend(self.config):
                    self.fail("Occupied port started backend")
        self.assertEqual(b"must not open or migrate", self.database.read_bytes())
        self.assertFalse((self.database.parent / "backups").exists())

    def test_lifecycle_authentication_and_read_only_status(self):
        with running_backend(self.config) as backend:
            self.assertEqual(("127.0.0.1", self.port), backend.server.server_address)
            self.assertIsNone(backend.backup)
            self.assertEqual(401, self.request()[0])
            code, payload = self.request(headers={"Authorization": "Bearer " + self.token})
            self.assertEqual(200, code)
            self.assertTrue(payload["capabilities"]["remote_access"])
            self.assertFalse(payload["capabilities"]["worker_enabled"])
            report = status(self.config)
            self.assertTrue(report["local_api_reachable"])
            self.assertNotIn(self.token, json.dumps(report))
            session = payload["token"]
            code, _ = self.request("POST", "/api/v1/decisions", "{}", {
                "Authorization": "Bearer " + self.token, "X-Review-Token": session,
                "Origin": "https://unapproved.example", "Content-Type": "application/json"})
            self.assertEqual(403, code)
            with self.assertRaisesRegex(RuntimeError, "holds this database lock"):
                with running_backend(self.config):
                    self.fail("Concurrent process acquired the DB")
        self.assertFalse(backend.thread.is_alive())
        self.assert_released()

    def test_wal_approval_and_original_schema_are_in_verified_backup_before_migration(self):
        connection = self.seed_approval()
        self.assertGreater(Path(str(self.database) + "-wal").stat().st_size, 0)
        with running_backend(self.config) as backend:
            with sqlite3.connect(backend.backup) as saved:
                self.assertEqual([("fictional:42", "approved")], saved.execute("SELECT * FROM saved_approval").fetchall())
                self.assertEqual([("ok",)], saved.execute("PRAGMA quick_check").fetchall())
                self.assertEqual([("saved_approval",)], saved.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            self.assertEqual(0o600, stat.S_IMODE(backend.backup.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(backend.backup.parent.stat().st_mode))
            self.assertEqual([("fictional:42", "approved")], connection.execute("SELECT * FROM saved_approval").fetchall())
        self.assertTrue(backend.backup.exists())
        self.assert_released()

    def test_failed_backup_leaves_database_unchanged_and_listener_closed(self):
        self.database.parent.mkdir()
        self.database.write_text("not SQLite")
        with self.assertRaises(sqlite3.Error):
            with running_backend(self.config):
                self.fail("Corrupt database started backend")
        self.assertEqual("not SQLite", self.database.read_text())
        self.assertEqual([], list((self.database.parent / "backups").iterdir()))
        self.assert_released()

    def test_probe_failure_and_body_exception_both_stop_backend(self):
        with patch("tailscale_backend._probe", side_effect=ValueError("fictional failed probe")):
            with self.assertRaisesRegex(ValueError, "failed probe"):
                with running_backend(self.config):
                    self.fail("Failed probe yielded backend")
        self.assert_released()
        with self.assertRaisesRegex(RuntimeError, "fictional caller failure"):
            with running_backend(self.config) as backend:
                raise RuntimeError("fictional caller failure")
        self.assertFalse(backend.thread.is_alive())
        self.assert_released()

    def test_backend_thread_stopping_is_detected(self):
        with running_backend(self.config) as backend:
            backend.server.shutdown()
            backend.thread.join(2)
            with self.assertRaisesRegex(RuntimeError, "stopped unexpectedly"):
                backend.check_running()
        self.assert_released()

    def test_partial_requests_timeout_and_connection_limit_recovers(self):
        with patch.object(tailscale_backend.TailnetReviewServer, "max_connections", 1), \
                patch.object(tailscale_backend.TailnetReviewServer, "request_timeout", 0.25):
            with running_backend(self.config):
                with socket.create_connection(("127.0.0.1", self.port), timeout=2) as partial:
                    partial.sendall(b"GET /api/v1/capabilities HTTP/1.1\r\n")
                    # Give the serving thread time to accept and reserve its slot.
                    time.sleep(0.05)
                    with socket.create_connection(("127.0.0.1", self.port), timeout=2) as excess:
                        self.assertEqual(b"", excess.recv(1))
                    self.assertEqual(b"", partial.recv(1))
                self.assertEqual(200, self.request(headers={"Authorization": "Bearer " + self.token})[0])
        self.assert_released()

    def test_shutdown_closes_partial_request_without_waiting_for_read_timeout(self):
        with running_backend(self.config) as backend:
            partial = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            self.addCleanup(partial.close)
            partial.sendall(b"GET /api/v1/capabilities HTTP/1.1\r\n")
            time.sleep(0.05)
            start = time.monotonic()
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(backend.thread.is_alive())
        self.assertEqual(b"", partial.recv(4096))
        self.assert_released()

    def test_shutdown_retains_database_lock_until_delayed_scheduling_tick_finishes(self):
        block_tick = threading.Event()
        tick_entered = threading.Event()
        release_tick = threading.Event()
        backend_ready = threading.Event()
        start_shutdown = threading.Event()
        finished = threading.Event()
        failures = []

        def delayed_tick(server):
            if block_tick.is_set():
                tick_entered.set()
                release_tick.wait(20)

        def own_backend():
            try:
                with running_backend(self.config):
                    backend_ready.set()
                    start_shutdown.wait(20)
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        with patch.object(tailscale_backend.TailnetReviewServer, "service_actions", delayed_tick):
            owner = threading.Thread(target=own_backend)
            owner.start()
            try:
                self.assertTrue(backend_ready.wait(5))
                block_tick.set()
                self.assertTrue(tick_entered.wait(5))
                start_shutdown.set()
                # Exceed the old stack helper's two five-second waits. An
                # unfinished tick must still own the database after that time.
                self.assertFalse(finished.wait(10.2))
                with self.assertRaisesRegex(RuntimeError, "holds this database lock"):
                    with database_lock(self.database):
                        self.fail("Lock released while scheduling tick still runs")
            finally:
                release_tick.set()
                start_shutdown.set()
                owner.join(5)
        self.assertFalse(owner.is_alive())
        self.assertEqual([], failures)
        self.assert_released()

    def test_cli_sigterm_and_sigint_release_port_lock_and_preserve_database(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                process = subprocess.Popen([sys.executable, "-B", str(Path(tailscale_backend.__file__)), "run",
                    "--db", str(self.database), "--access-config", str(self.access),
                    "--credential-file", str(self.credential), "--port", str(self.port)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            self.fail("Supervisor exited before readiness: " + process.communicate()[1])
                        try:
                            status(self.config)
                            break
                        except (OSError, http.client.HTTPException):
                            if time.monotonic() > deadline:
                                self.fail("Supervisor did not become ready")
                            time.sleep(0.05)
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=10)
                    self.assertEqual(0, process.returncode, errors)
                    self.assertIn("Tailscale backend stopped", output)
                    self.assertNotIn(self.token, output + errors)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
                self.assertTrue(self.database.exists())
                self.assert_released()


if __name__ == "__main__":
    unittest.main()
