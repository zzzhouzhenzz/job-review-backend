"""Stack configuration, SQLite preservation, supervision, and optional real TLS."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import device_stack
from device_stack import StackConfig, backup_database, database_lock, load_config, running_stack
from test_tls_gateway import CADDY, OPENSSL, DirectHTTPSConnection, make_certificates, unused_port
from tls_gateway import GatewayConfig


class DeviceStackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "stack.json"
        self.database = self.root / "state" / "review.sqlite3"
        self.backend_port = unused_port()
        self.token = secrets.token_urlsafe(32)
        self.access = self.root / "access.json"
        self.access.write_text(json.dumps({"schema_version": 1, "public_origin": "https://localhost:8443",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest()}))
        self.access.chmod(0o600)
        self.data = {"schema_version": 1, "database": "state/review.sqlite3", "gateway": {
            "access_config": "access.json", "credential_file": "device.txt", "cert_file": "cert.pem",
            "key_file": "key.pem", "backend_port": self.backend_port, "listen_port": 8443,
            "caddy": "bin/caddy"}}
        self.write_config()
        self.config = load_config(self.path)
        self.gateway = Mock()
        self.gateway.poll.return_value = None
        self.gateway_exited = False

    def write_config(self):
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    @contextmanager
    def fake_gateway(self, config, **kwargs):
        try:
            yield self.gateway
        finally:
            self.gateway_exited = True

    def assert_port_released(self):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.backend_port))

    def test_config_resolves_paths_and_accepts_gateway_defaults(self):
        self.assertEqual(self.database.resolve(), self.config.database)
        self.assertEqual(self.root / "access.json", self.config.gateway.access_config)
        self.assertEqual(str(self.root / "bin" / "caddy"), self.config.gateway.caddy)
        self.assertEqual("127.0.0.1", self.config.gateway.listen_address)
        del self.data["gateway"]["caddy"]
        self.data["gateway"]["ca_file"] = "ca.pem"
        self.write_config()
        result = load_config(self.path)
        self.assertEqual("caddy", result.gateway.caddy)
        self.assertEqual(self.root / "ca.pem", result.gateway.ca_file)

    def test_config_requires_private_regular_file(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            load_config(self.path)
        self.path.chmod(0o600)
        linked = self.root / "link.json"
        linked.symlink_to(self.path)
        with self.assertRaises(OSError):
            load_config(linked)

    def test_loaded_gateway_secret_paths_retain_symlink_rejection(self):
        credential = self.root / "device.txt"
        credential.write_text(self.token + "\n")
        credential.chmod(0o600)
        key = self.root / "key.pem"
        key.write_text("unused private key")
        key.chmod(0o600)
        for field in ("access_config", "credential_file", "key_file"):
            with self.subTest(field=field):
                original = self.data["gateway"][field]
                linked = self.root / (field + "-link")
                linked.symlink_to(self.root / original)
                self.data["gateway"][field] = linked.name
                self.write_config()
                config = load_config(self.path)
                self.assertTrue(getattr(config.gateway, field).is_symlink())
                with self.assertRaises(OSError):
                    device_stack.check(config)
                self.data["gateway"][field] = original

    def test_config_rejects_unknown_duplicate_and_invalid_fields(self):
        for mutate in (lambda d: d.update(extra=True), lambda d: d.update(schema_version=True),
                       lambda d: d.update(database=""), lambda d: d["gateway"].update(extra=1),
                       lambda d: d["gateway"].update(backend_port=True),
                       lambda d: d["gateway"].update(caddy=[]),
                       lambda d: d["gateway"].update(listen_address=None)):
            data = json.loads(json.dumps(self.data))
            mutate(data)
            self.path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_config(self.path)
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_config(self.path)

    def test_local_check_does_not_open_database_or_listener(self):
        with patch("device_stack.tls_gateway.prepare", return_value="prepared") as prepare, \
                patch("device_stack.tls_gateway._validate") as validate, \
                patch("device_stack.ReviewServer") as server:
            self.assertEqual("prepared", device_stack.check(self.config))
        prepare.assert_called_once_with(self.config.gateway)
        validate.assert_called_once()
        server.assert_not_called()
        self.assertFalse(self.database.parent.exists())

    def test_status_reads_health_without_database_access(self):
        prepared = Mock()
        prepared.access.public_origin = "https://localhost:8443"
        prepared.access.public_host = "localhost:8443"
        prepared.credential = self.token
        client = Mock()
        response = client.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({"schema_version": 1,
            "health": {"status": "disabled", "ready": False}}).encode()
        with patch("device_stack.tls_gateway.prepare", return_value=prepared), \
                patch("device_stack._local_tls_connection", return_value=client), \
                patch("device_stack.tls_gateway._read_probe") as probe:
            report = device_stack.status(self.config)
            self.assertTrue(report["local_api_reachable"])
            self.assertFalse(report["worker_ready"])
            self.assertEqual("disabled", report["worker_status"])
            self.assertEqual([False, True], [call.kwargs["authenticated"] for call in probe.call_args_list])
            self.assertNotIn(self.token, json.dumps(report))
            for malformed in (None, [], {}, {"schema_version": True, "health": {"status": "ready", "ready": True}},
                              {"schema_version": 1, "health": {"status": "ready", "ready": 1}}):
                response.read.return_value = json.dumps(malformed).encode()
                with self.assertRaisesRegex(ValueError, "valid v1 readiness"):
                    device_stack.status(self.config)
        self.assertFalse(self.database.parent.exists())
        client.close.assert_called()

    def test_database_lock_excludes_second_stack_and_releases(self):
        with database_lock(self.database):
            with self.assertRaisesRegex(RuntimeError, "holds this database lock"):
                with database_lock(self.database):
                    self.fail("Second lock was acquired")
        with database_lock(self.database):
            pass
        self.assertEqual(0o600, stat.S_IMODE(self.database.with_name(
            self.database.name + ".device-stack.lock").stat().st_mode))

    def test_lock_rejects_symlink(self):
        self.database.parent.mkdir()
        lock = self.database.with_name(self.database.name + ".device-stack.lock")
        lock.symlink_to(self.access)
        before = self.access.read_bytes()
        with self.assertRaises(OSError):
            with database_lock(self.database):
                pass
        self.assertEqual(before, self.access.read_bytes())

    def test_backup_preserves_committed_wal_and_approval_data(self):
        self.database.parent.mkdir()
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE approvals (job TEXT PRIMARY KEY, decision TEXT, snapshot TEXT)")
        connection.execute("INSERT INTO approvals VALUES (?, ?, ?)", ("fictional:42", "approved", '{"title":"Engineer"}'))
        connection.commit()
        self.assertTrue(Path(str(self.database) + "-wal").stat().st_size)
        original_file = self.database.read_bytes()
        original_rows = connection.execute("SELECT * FROM approvals").fetchall()
        backup = backup_database(self.database)
        with sqlite3.connect(backup) as copy:
            self.assertEqual(original_rows, copy.execute("SELECT * FROM approvals").fetchall())
            self.assertEqual([("ok",)], copy.execute("PRAGMA quick_check").fetchall())
        self.assertEqual(original_rows, connection.execute("SELECT * FROM approvals").fetchall())
        self.assertEqual(original_file, self.database.read_bytes())
        self.assertEqual(0o700, stat.S_IMODE(backup.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(backup.stat().st_mode))
        self.assertNotEqual(backup, backup_database(self.database))

    def test_backup_absent_database_is_noop_and_invalid_database_fails(self):
        self.assertIsNone(backup_database(self.database))
        self.assertFalse(self.database.parent.exists())
        self.database.parent.mkdir()
        self.database.write_text("not a database")
        with self.assertRaises(sqlite3.Error):
            backup_database(self.database)
        self.assertEqual([], list((self.database.parent / "backups").iterdir()))
        self.assertEqual("not a database", self.database.read_text())

    def test_backup_rejects_insecure_directory(self):
        self.database.parent.mkdir()
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE test (id INTEGER)")
        backups = self.database.parent / "backups"
        backups.mkdir(mode=0o755)
        backups.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            backup_database(self.database)

    def test_failed_tls_preflight_does_not_create_database_directory(self):
        with patch("device_stack.check", side_effect=ValueError("invalid certificate")):
            with self.assertRaisesRegex(ValueError, "invalid certificate"):
                with running_stack(self.config):
                    pass
        self.assertFalse(self.database.parent.exists())

    def test_occupied_backend_port_does_not_open_or_back_up_database(self):
        self.database.parent.mkdir()
        self.database.write_bytes(b"existing database must be untouched")
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.backend_port))
            occupied.listen()
            with patch("device_stack.check"), patch("device_stack.ReviewServer") as server:
                with self.assertRaises(OSError):
                    with running_stack(self.config):
                        pass
        server.assert_not_called()
        self.assertFalse((self.database.parent / "backups").exists())
        self.assertEqual(b"existing database must be untouched", self.database.read_bytes())

    def test_supervisor_starts_authenticated_backend_and_cleans_up(self):
        with patch("device_stack.check"), patch("device_stack.tls_gateway.running_gateway", self.fake_gateway):
            with running_stack(self.config) as stack:
                stack.check_running()
                self.assertIsNone(stack.backup)
                client = http.client.HTTPConnection("127.0.0.1", self.backend_port, timeout=2)
                client.request("GET", "/api/v1/capabilities")
                response = client.getresponse()
                self.assertEqual(401, response.status)
                response.read()
                client.close()
                self.assertFalse(stack.server.worker_capabilities()["worker_enabled"])
                self.assertTrue(stack.server.worker_capabilities()["remote_access"])
            self.assertFalse(stack.thread.is_alive())
            self.assertTrue(self.gateway_exited)
        self.assert_port_released()
        with database_lock(self.database):
            pass

    def test_gateway_startup_failure_stops_backend(self):
        with patch("device_stack.check"), patch("device_stack.tls_gateway.running_gateway",
                side_effect=RuntimeError("gateway startup failed")):
            with self.assertRaisesRegex(RuntimeError, "gateway startup failed"):
                with running_stack(self.config):
                    pass
        self.assert_port_released()

    def test_gateway_exit_is_detected_and_both_services_stop(self):
        with patch("device_stack.check"), patch("device_stack.tls_gateway.running_gateway", self.fake_gateway):
            with self.assertRaisesRegex(RuntimeError, "status 7"):
                with running_stack(self.config) as stack:
                    self.gateway.poll.return_value = 7
                    stack.check_running()
            self.assertTrue(self.gateway_exited)
            self.assertFalse(stack.thread.is_alive())
        self.assert_port_released()

    def test_backend_exit_and_errors_are_detected(self):
        with patch("device_stack.check"), patch("device_stack.tls_gateway.running_gateway", self.fake_gateway):
            with running_stack(self.config) as stack:
                stack.server.shutdown()
                stack.thread.join(2)
                with self.assertRaisesRegex(RuntimeError, "stopped unexpectedly"):
                    stack.check_running()
                original = RuntimeError("fictional backend error")
                stack.failures.append(original)
                with self.assertRaisesRegex(RuntimeError, "serving thread failed") as caught:
                    stack.check_running()
                self.assertIs(original, caught.exception.__cause__)
        self.assertTrue(self.gateway_exited)


@unittest.skipUnless(CADDY and OPENSSL, "Real stack TLS test requires JOB_REVIEW_CADDY and openssl")
class RealDeviceStackTests(unittest.TestCase):
    def test_one_supervisor_runs_verified_https_and_preserves_database_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca, certificate, key = make_certificates(root)
            backend_port, tls_port = unused_port(), unused_port()
            token = secrets.token_urlsafe(32)
            access, credential = root / "access.json", root / "token.txt"
            access.write_text(json.dumps({"schema_version": 1, "public_origin": f"https://localhost:{tls_port}",
                "token_sha256": hashlib.sha256(token.encode()).hexdigest()}))
            credential.write_text(token + "\n")
            access.chmod(0o600)
            credential.chmod(0o600)
            config = StackConfig(root / "review.sqlite3", GatewayConfig(access, credential, certificate, key,
                backend_port=backend_port, listen_port=tls_port, caddy=CADDY, ca_file=ca))
            with (root / "caddy.log").open("wb") as logs:
                for attempt in range(2):
                    with running_stack(config, log_output=logs) as stack:
                        self.assertEqual(attempt > 0, stack.backup is not None)
                        client = DirectHTTPSConnection("localhost", tls_port,
                            context=ssl.create_default_context(cafile=str(ca)), timeout=5)
                        try:
                            client.request("GET", "/api/v1/capabilities", headers={"Authorization": "Bearer " + token})
                            response = client.getresponse()
                            self.assertEqual(200, response.status)
                            result = json.loads(response.read())
                            self.assertTrue(result["capabilities"]["remote_access"])
                            self.assertFalse(result["capabilities"]["worker_enabled"])
                        finally:
                            client.close()
                        report = device_stack.status(config)
                        self.assertTrue(report["local_api_reachable"])
                        self.assertFalse(report["worker_ready"])
                    self.assertIsNotNone(stack.gateway.poll())
                    self.assertFalse(stack.thread.is_alive())

            # Exercise the actual foreground entry point and its SIGTERM path.
            configuration = root / "stack.json"
            configuration.write_text(json.dumps({"schema_version": 1, "database": str(config.database),
                "gateway": {"access_config": str(access), "credential_file": str(credential),
                    "cert_file": str(certificate), "key_file": str(key), "backend_port": backend_port,
                    "listen_port": tls_port, "caddy": CADDY, "ca_file": str(ca)}}))
            configuration.chmod(0o600)
            process = subprocess.Popen([sys.executable, "-B", str(Path(device_stack.__file__)),
                "run", "--config", str(configuration)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while True:
                    self.assertIsNone(process.poll(), "Stack CLI exited before its HTTPS API was ready")
                    try:
                        device_stack.status(config)
                        break
                    except (OSError, http.client.HTTPException):
                        if time.monotonic() >= deadline:
                            self.fail("Stack CLI did not start HTTPS within 20 seconds")
                        time.sleep(0.1)
                process.terminate()
                output, diagnostics = process.communicate(timeout=15)
                self.assertEqual(0, process.returncode, diagnostics)
                self.assertIn("Device stack stopped.", output)
                self.assertNotIn(token, output + diagnostics)
                for port in (backend_port, tls_port):
                    with socket.socket() as released:
                        released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        released.bind(("127.0.0.1", port))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
