"""Authenticated gateway contract. Temporary credentials and loopback ports only."""
import hashlib
import http.client
import io
import json
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from server import ReviewServer
from start import probe, main as start_main
from test_store import catalog


class AuthenticatedServerTests(unittest.TestCase):
    origin = "https://fictional-device.example.ts.net"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "access.json"
        self.credential = secrets.token_urlsafe(32)
        self.save_config()
        self.start()
        self.server.store.import_catalog(catalog(), "fictional authentication tests")

    def save_config(self):
        self.config.write_text(json.dumps({"schema_version": 1, "public_origin": self.origin,
            "token_sha256": hashlib.sha256(self.credential.encode()).hexdigest()}))
        self.config.chmod(0o600)

    def start(self, tls_context=None):
        self.server = ReviewServer(("127.0.0.1", 0), self.root / "db.sqlite3", access_config=self.config)
        if tls_context is not None:
            # Test fixture only. The deployed HTTPS endpoint is a separate gateway.
            self.server.socket = tls_context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None, duplicates=()):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        values = {"Host": self.server.access.public_host, "Authorization": "Bearer " + self.credential}
        if body is not None:
            values.update({"Content-Type": "application/json", "Origin": self.origin,
                           "X-Review-Token": self.server.token})
        for key, value in (headers or {}).items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        encoded = None if body is None else json.dumps(body).encode()
        try:
            client.putrequest("GET" if body is None else "POST", path, skip_host=True)
            for key, value in values.items():
                client.putheader(key, value)
            for key, value in duplicates:
                client.putheader(key, value)
            if encoded is not None:
                client.putheader("Content-Length", str(len(encoded)))
            client.endheaders(encoded)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            client.close()

    def decision(self):
        status, _, feed = self.request("/api/v1/feed")
        self.assertEqual(200, status)
        card = feed["cards"][0]
        return {"request_id": "fictional-secure-apply", "card_id": card["id"],
                "card_revision": card["revision"], "action": "apply", "selected": [card["jobs"][0]["key"]]}

    def test_all_reads_require_device_auth_before_any_state_or_token(self):
        self.server.store.state = Mock(side_effect=AssertionError("Authentication must run first"))
        self.server.store.feed = Mock(side_effect=AssertionError("Authentication must run first"))
        for path in ("/api/v1/health", "/api/v1/capabilities", "/api/v1/feed", "/api/v1/questions",
                     "/api/v1/processes", "/api/v1/tasks", "/api/state", "/api/export", "/", "/app.js", "/unknown"):
            for credential in (None, "Bearer " + "x" * 43):
                with self.subTest(path=path, credential=credential is not None):
                    status, headers, body = self.request(path, headers={"Authorization": credential})
                    self.assertEqual(401, status)
                    self.assertIn("Bearer", headers["WWW-Authenticate"])
                    self.assertNotIn("Location", headers)
                    self.assertNotIn("token", body)
                    if path.startswith("/api/v1/"):
                        self.assertEqual({"code": "unauthorized", "message": "A valid device credential is required.",
                                          "reason": None, "commit_status": "not_committed"}, body["error"])

    def test_loopback_host_and_forwarded_identity_cannot_bypass_auth(self):
        for host in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}", self.server.access.public_host):
            status, _, _ = self.request("/api/v1/health", headers={"Authorization": None, "Host": host,
                "Forwarded": "for=127.0.0.1;proto=https", "X-Forwarded-Proto": "https",
                "Tailscale-User-Login": "fictional@example.invalid"})
            self.assertEqual(401, status)

    def test_authenticated_ios_reads_and_gateway_host_rewrite_work(self):
        self.server.orchestrator.readiness.wait(2)
        for host in (self.server.access.public_host, f"127.0.0.1:{self.server.server_port}"):
            status, _, body = self.request("/api/v1/capabilities", headers={"Host": host})
            self.assertEqual(200, status)
            self.assertTrue(body["capabilities"]["remote_access"])
            self.assertEqual(body["capabilities"], self.request("/api/v1/feed")[2]["capabilities"])
        for route in ("/api/state", "/api/export", "/api/v1/health"):
            self.assertEqual(200, self.request(route)[0])
        self.assertNotIn("token", self.request("/api/export")[2])

    def test_unauthorized_commands_never_execute_or_save(self):
        for path, method in (("/api/v1/decisions", "receive_decision"), ("/api/v1/answers", "answer_questions"),
                             ("/api/v1/tasks/cancel", "cancel_task_command"), ("/api/card", "review_card")):
            operation = Mock(side_effect=AssertionError("No unauthorized mutation"))
            with patch.object(self.server.store, method, operation):
                self.assertEqual(401, self.request(path, {}, headers={"Authorization": None})[0])
                operation.assert_not_called()

    def test_exact_origin_session_token_host_and_single_headers_required(self):
        payload = self.decision()
        for headers in ({"Origin": self.origin + "/"}, {"Origin": "https://other.example.ts.net"},
                        {"Origin": f"http://127.0.0.1:{self.server.server_port}"}, {"Origin": None},
                        {"X-Review-Token": "bad"}, {"X-Review-Token": None}, {"Host": "other.example.ts.net"}):
            self.assertEqual(403, self.request("/api/v1/decisions", payload, headers=headers)[0])
        for header, value, code in (("Authorization", "Bearer " + self.credential, 401),
                                    ("Host", self.server.access.public_host, 403),
                                    ("Origin", self.origin, 403), ("X-Review-Token", self.server.token, 403)):
            self.assertEqual(code, self.request("/api/v1/decisions", payload, duplicates=[(header, value)])[0])
        self.assertEqual([], self.server.store.processes())

    def test_same_request_survives_auth_rotation_and_restart_without_duplicate_apply(self):
        payload = self.decision()
        first = self.request("/api/v1/decisions", payload)[2]
        previous_credential, previous_session = self.credential, self.server.token
        self.stop()
        self.credential = secrets.token_urlsafe(32)
        self.save_config()
        self.start()
        self.assertEqual(401, self.request("/api/v1/capabilities", headers={"Authorization": "Bearer " + previous_credential})[0])
        self.assertEqual(403, self.request("/api/v1/decisions", payload, headers={"X-Review-Token": previous_session})[0])
        fresh_session = self.request("/api/v1/capabilities")[2]["token"]
        status, _, replay = self.request("/api/v1/decisions", payload, headers={"X-Review-Token": fresh_session})
        self.assertEqual(200, status)
        self.assertEqual({k: v for k, v in first.items() if k != "token"}, {k: v for k, v in replay.items() if k != "token"})
        self.assertEqual(1, len(self.server.store.processes()))

    def test_authenticated_request_log_omits_remote_path_and_credential(self):
        with redirect_stdout(io.StringIO()) as capture:
            self.request("/api/v1/health?accidental_secret=do-not-log")
        self.assertNotIn("do-not-log", capture.getvalue())
        self.assertNotIn(self.credential, capture.getvalue())

    def test_malformed_request_does_not_echo_or_log_query_secrets(self):
        with redirect_stdout(io.StringIO()) as capture:
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as client:
                client.sendall(b"GET /api/v1/health?accidental_secret=do-not-log extra HTTP/1.1\r\n\r\n")
                response = b""
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response += chunk
        self.assertIn(b"400 Bad Request", response)
        self.assertNotIn(b"do-not-log", response)
        self.assertNotIn("do-not-log", capture.getvalue())

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture requires openssl")
    def test_authenticated_contract_over_tls_with_certificate_validation(self):
        config, cert, key = (self.root / name for name in ("tls.cnf", "tls.crt", "tls.key"))
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                          "[dn]\nCN=localhost\n[ext]\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n"
                          "basicConstraints=critical,CA:TRUE\n")
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1", "-config", str(config)],
                       check=True, capture_output=True, timeout=15)
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(str(cert), str(key))
        self.stop()
        self.start(tls_context)
        untrusted = http.client.HTTPSConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                untrusted.request("GET", "/api/v1/capabilities")
        finally:
            untrusted.close()
        trusted_context = ssl.create_default_context(cafile=str(cert))
        client = http.client.HTTPSConnection("127.0.0.1", self.server.server_port,
                                            timeout=3, context=trusted_context)
        try:
            client.request("GET", "/api/v1/capabilities", headers={"Authorization": "Bearer " + self.credential})
            response = client.getresponse()
            self.assertEqual(200, response.status)
            body = json.loads(response.read())
            self.assertTrue(body["capabilities"]["remote_access"])
        finally:
            client.close()

    def test_missing_access_config_fails_before_opening_database(self):
        absent_db = self.root / "must-not-create.sqlite3"
        with self.assertRaises(OSError):
            ReviewServer(("127.0.0.1", 0), absent_db, access_config=self.root / "absent.json")
        self.assertFalse(absent_db.exists())

    def test_local_launcher_does_not_try_to_replace_authenticated_server(self):
        self.assertEqual("protected", probe(self.server.server_port))
        with patch("start.probe", return_value="protected"), patch("start.subprocess.Popen") as spawn:
            with patch("sys.argv", ["start.py"]), self.assertRaisesRegex(SystemExit, "authentication"):
                start_main()
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
