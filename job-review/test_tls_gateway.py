"""Dedicated gateway checks with fictional data and temporary TLS credentials.

Set JOB_REVIEW_CADDY to an existing Caddy binary to run the real proxy checks.
The fixture CA is trusted only by these test clients, never installed globally.
"""
import hashlib
import http.client
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from server import MAX_BODY, Handler, ReviewServer
from test_store import catalog
from tls_gateway import GatewayConfig, check, prepare, running_gateway


CADDY = os.environ.get("JOB_REVIEW_CADDY")
OPENSSL = shutil.which("openssl")


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_certificates(root):
    """A CA and separate server leaf, both confined to the temporary directory."""
    ca_config = root / "ca.cnf"
    ca_config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                         "[dn]\nCN=Fictional Job Review Test CA\n[ext]\n"
                         "basicConstraints=critical,CA:TRUE\n"
                         "keyUsage=critical,keyCertSign,cRLSign\n")
    leaf_config = root / "leaf.cnf"
    leaf_config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nreq_extensions=ext\n"
                           "[dn]\nCN=localhost\n[ext]\n"
                           "basicConstraints=critical,CA:FALSE\n"
                           "keyUsage=critical,digitalSignature,keyEncipherment\n"
                           "extendedKeyUsage=serverAuth\n"
                           "subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    ca, ca_key, cert, key, csr = [root / name for name in
                                ("ca.pem", "ca.key", "leaf.pem", "leaf.key", "leaf.csr")]
    commands = [
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-config", str(ca_config), "-keyout", str(ca_key), "-out", str(ca)],
        [OPENSSL, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", str(leaf_config),
         "-keyout", str(key), "-out", str(csr)],
        [OPENSSL, "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key),
         "-CAcreateserial", "-days", "2", "-extfile", str(leaf_config), "-extensions", "ext",
         "-out", str(cert)],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True, timeout=20)
    ca_key.chmod(0o600)
    key.chmod(0o600)
    return ca, cert, key


class DirectHTTPSConnection(http.client.HTTPSConnection):
    """Keep DNS and environment proxy settings outside the local TLS fixture."""
    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


@unittest.skipUnless(CADDY and OPENSSL,
                     "Real TLS gateway tests require JOB_REVIEW_CADDY and openssl")
class TLSGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_temp = tempfile.TemporaryDirectory()
        cls.ca, cls.cert, cls.key = make_certificates(Path(cls.cert_temp.name))

    @classmethod
    def tearDownClass(cls):
        cls.cert_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.gateway_port = unused_port()
        self.origin = "https://localhost:" + str(self.gateway_port)
        self.token = secrets.token_urlsafe(32)
        self.access_path, self.credential_path = self.root / "access.json", self.root / "device.txt"
        self.write_access()
        self.credential_path.write_text(self.token + "\n")
        self.credential_path.chmod(0o600)
        self.key = self.root / "server.key"
        self.key.write_bytes(type(self).key.read_bytes())
        self.key.chmod(0o600)
        self.server = None
        self.start_backend()
        self.addCleanup(self.stop_backend)
        self.gateway_logs = (self.root / "caddy.log").open("wb")
        self.addCleanup(self.gateway_logs.close)
        self.server.store.import_catalog(catalog(), "fictional gateway tests")
        self.server.orchestrator.readiness.wait(2)
        self.config = GatewayConfig(access_config=self.access_path, credential_file=self.credential_path,
            cert_file=self.cert, key_file=self.key, backend_port=self.server.server_port,
            listen_address="127.0.0.1", listen_port=self.gateway_port, caddy=CADDY, ca_file=self.ca)

    def write_access(self, origin=None):
        self.access_path.write_text(json.dumps({"schema_version": 1, "public_origin": origin or self.origin,
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest()}))
        self.access_path.chmod(0o600)

    def start_backend(self, port=0, authenticated=True):
        self.server = ReviewServer(("127.0.0.1", port), self.root / "state.sqlite3",
                                  access_config=self.access_path if authenticated else None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_backend(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(3)
            self.server = None

    def request(self, path="/api/v1/capabilities", *, method="GET", body=None,
                headers=None, duplicates=(), trusted=True, hostname="localhost"):
        context = ssl.create_default_context(cafile=str(self.ca) if trusted else None)
        client = DirectHTTPSConnection(hostname, self.gateway_port, context=context, timeout=5)
        values = {"Host": self.origin[len("https://"):], "Authorization": "Bearer " + self.token}
        if body is not None:
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            values.update({"Content-Type": "application/json", "Origin": self.origin,
                           "Content-Length": str(len(body)), "X-Review-Token": self.server.token})
        for key, value in (headers or {}).items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        try:
            client.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in values.items():
                client.putheader(key, value)
            for key, value in duplicates:
                client.putheader(key, value)
            client.endheaders(body)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def test_prepare_and_check_validate_real_caddy_configuration(self):
        prepared = prepare(self.config)
        self.assertEqual(self.origin, prepared.access.public_origin)
        self.assertEqual(self.gateway_port, prepared.listen_port)
        self.assertEqual("localhost", prepared.hostname)
        self.assertNotIn(self.token, prepared.caddyfile)
        self.assertNotIn(hashlib.sha256(self.token.encode()).hexdigest(), prepared.caddyfile)
        check(self.config)

    def test_verified_tls_reads_authentication_and_no_secret_disclosure(self):
        with running_gateway(self.config, log_output=self.gateway_logs):
            status, headers, raw = self.request()
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["capabilities"]["remote_access"])
            self.assertEqual("no-store", headers["Cache-Control"])
            for auth in (None, "Bearer " + "x" * 43):
                with self.subTest(auth_present=auth is not None):
                    status, headers, raw = self.request(headers={"Authorization": auth})
                    self.assertEqual(401, status)
                    self.assertEqual("unauthorized", json.loads(raw)["error"]["code"])
                    self.assertNotIn("token", json.loads(raw))
                    self.assertNotIn("Location", headers)
            status, _, _ = self.request(duplicates=[("Authorization", "Bearer " + self.token)])
            self.assertEqual(401, status)
            for path in ("/api/state", "/api/export", "/api/v1/health", "/api/v1/questions",
                         "/api/v1/processes", "/api/v1/tasks"):
                with self.subTest(path=path):
                    self.assertEqual(200, self.request(path)[0])
                    self.assertEqual(401, self.request(path, headers={"Authorization": None})[0])

    def test_untrusted_and_wrong_hostname_certificates_fail(self):
        with running_gateway(self.config, log_output=self.gateway_logs):
            with self.assertRaises(ssl.SSLError):
                self.request(trusted=False)
            with self.assertRaises(ssl.SSLError):
                self.request(hostname="different.example.invalid")

    def test_plain_http_never_reaches_backend(self):
        with running_gateway(self.config, log_output=self.gateway_logs):
            client = http.client.HTTPConnection("127.0.0.1", self.gateway_port, timeout=5)
            try:
                client.request("GET", "/api/v1/feed", headers={"Host": self.origin[8:],
                               "Authorization": "Bearer " + self.token})
                response = client.getresponse()
                self.assertGreaterEqual(response.status, 400)
                self.assertNotIn(b'"cards"', response.read())
            except (ConnectionError, http.client.RemoteDisconnected):
                pass  # Rejecting plaintext by closing is also valid.
            finally:
                client.close()

    def test_host_origin_and_forwarded_headers_cannot_bypass_checks(self):
        with running_gateway(self.config, log_output=self.gateway_logs):
            for host in ("evil.example.invalid", "localhost", "localhost:1",
                         f"127.0.0.1:{self.server.server_port}"):
                with self.subTest(host=host):
                    self.assertGreaterEqual(self.request(headers={"Host": host})[0], 400)
            for origin in (None, self.origin + "/", "https://other.example.invalid"):
                with self.subTest(origin=origin):
                    self.assertEqual(403, self.request("/api/v1/decisions", method="POST", body={},
                                                      headers={"Origin": origin})[0])
            status, _, _ = self.request(headers={"Authorization": None,
                "Forwarded": "for=127.0.0.1;proto=https", "X-Forwarded-Proto": "https",
                "Tailscale-User-Login": "fictional@example.invalid"})
            self.assertEqual(401, status)
            self.assertEqual([], self.server.store.processes())

    def test_exact_post_bytes_and_query_reach_backend_and_replay_commits_once(self):
        received = []
        sent = []
        original = Handler.v1_command
        original_send = Handler.send

        def observe(handler, path):
            # Read once, then return the exact bytes to the normal parser.
            import io
            raw = handler.rfile.read(int(handler.headers["Content-Length"]))
            received.append((handler.path, raw, handler.headers["Origin"],
                             handler.headers["Authorization"], handler.headers["X-Review-Token"]))
            handler.rfile = io.BytesIO(raw)
            return original(handler, path)

        def observe_response(handler, status, body, *args, **kwargs):
            if handler.path.startswith("/api/v1/decisions"):
                sent.append((status, body))
            return original_send(handler, status, body, *args, **kwargs)

        with running_gateway(self.config, log_output=self.gateway_logs):
            card = json.loads(self.request("/api/v1/feed")[2])["cards"][0]
            payload = {"request_id": "fictional-tls-apply", "card_id": card["id"],
                       "card_revision": card["revision"], "action": "apply",
                       "selected": [card["jobs"][0]["key"]]}
            raw = (json.dumps(payload, indent=3) + "\n").encode()
            path = "/api/v1/decisions?fixture=exact%20bytes"
            with patch.object(Handler, "v1_command", observe), patch.object(Handler, "send", observe_response):
                first = self.request(path, method="POST", body=raw)
                second = self.request(path, method="POST", body=raw)
            self.assertEqual(200, first[0])
            self.assertEqual(json.loads(first[2]), json.loads(second[2]))
            self.assertEqual([(first[0], first[2]), (second[0], second[2])], sent)
            self.assertEqual(1, len(self.server.store.processes()))
            expected = (path, raw, self.origin, "Bearer " + self.token, self.server.token)
            self.assertEqual([expected, expected], received)

    def test_http2_apply_replay_uses_one_saved_intent(self):
        curl = shutil.which("curl")
        if not curl:
            self.skipTest("HTTP/2 test requires curl")
        version = subprocess.run([curl, "--version"], capture_output=True, text=True,
                                 check=True, timeout=3).stdout
        if "HTTP2" not in version:
            self.skipTest("This curl build does not support HTTP/2")

        def request(path, body_path=None):
            # All credentials go through stdin, never the process arguments.
            settings = ["http2", "silent", "show-error", 'max-time = "5"', 'proxy = ""',
                        "cacert = " + json.dumps(str(self.ca)),
                        "resolve = " + json.dumps("localhost:" + str(self.gateway_port) + ":127.0.0.1"),
                        "url = " + json.dumps(self.origin + path),
                        'write-out = "__GATEWAY_META__%{http_version}|%{http_code}"',
                        "header = " + json.dumps("Authorization: Bearer " + self.token)]
            if body_path:
                settings.extend(["data-binary = " + json.dumps("@" + str(body_path)),
                                 'header = "Content-Type: application/json"',
                                 "header = " + json.dumps("Origin: " + self.origin),
                                 "header = " + json.dumps("X-Review-Token: " + self.server.token)])
            result = subprocess.run([curl, "--config", "-"], input="\n".join(settings) + "\n",
                                    capture_output=True, text=True, timeout=8, check=True)
            body, metadata = result.stdout.rsplit("__GATEWAY_META__", 1)
            protocol, status = metadata.split("|")
            self.assertEqual("2", protocol)
            self.assertEqual("200", status)
            return json.loads(body)

        with running_gateway(self.config, log_output=self.gateway_logs):
            card = request("/api/v1/feed")["cards"][0]
            payload = {"request_id": "fictional-http2-apply", "card_id": card["id"],
                       "card_revision": card["revision"], "action": "apply",
                       "selected": [card["jobs"][0]["key"]]}
            body_path = self.root / "http2-decision.json"
            body_path.write_text(json.dumps(payload, indent=2) + "\n")
            first = request("/api/v1/decisions", body_path)
            self.assertEqual(first, request("/api/v1/decisions", body_path))
            self.assertEqual(1, len(self.server.store.processes()))

    def test_unsupported_routes_methods_and_oversized_requests_do_not_mutate(self):
        with running_gateway(self.config, log_output=self.gateway_logs):
            for method, path in (("GET", "/"), ("GET", "/app.js"), ("GET", "/api/v1/no-such-route"),
                                 ("DELETE", "/api/v1/feed"), ("POST", "/api/no-such-route")):
                with self.subTest(method=method, path=path):
                    status = self.request(path, method=method, body=b"{}" if method == "POST" else None)[0]
                    self.assertGreaterEqual(status, 400)
            # Require early rejection from the declared length, without sending
            # a large body into a socket which is correctly being closed.
            status = self.request("/api/v1/decisions", method="POST", body=b"",
                                  headers={"Content-Length": str(MAX_BODY + 1)})[0]
            self.assertIn(status, (400, 413))
            self.assertEqual([], self.server.store.processes())

    def test_default_local_mode_after_restart_stays_inaccessible(self):
        backend_port = self.server.server_port
        with running_gateway(self.config, log_output=self.gateway_logs):
            self.assertEqual(200, self.request()[0])
            self.stop_backend()
            self.start_backend(backend_port, authenticated=False)
            self.assertEqual(403, self.request()[0])
            self.assertEqual(403, self.request("/api/v1/feed", headers={"Authorization": None})[0])

    def test_preflight_rejects_unprotected_backend(self):
        backend_port = self.server.server_port
        self.stop_backend()
        self.start_backend(backend_port, authenticated=False)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            check(self.config)

    def test_preflight_rejects_wrong_backend_origin(self):
        # Configuration is loaded once by the backend; change only the gateway input.
        other_public_port = unused_port()
        while other_public_port in (self.gateway_port, self.server.server_port):
            other_public_port = unused_port()
        self.write_access("https://localhost:" + str(other_public_port))
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            check(self.config)

    def test_preflight_rejects_credentials_not_matching_access_config(self):
        self.credential_path.write_text(secrets.token_urlsafe(32) + "\n")
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            check(self.config)

    def test_preflight_rejects_permissive_credential_file(self):
        self.credential_path.chmod(0o644)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            check(self.config)

    def test_preflight_rejects_permissive_private_key_file(self):
        self.key.chmod(0o644)
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            check(self.config)

    def test_preflight_rejects_key_not_matching_certificate(self):
        self.key.write_bytes((Path(type(self).cert_temp.name) / "ca.key").read_bytes())
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            check(self.config)

    def test_untrusted_certificate_fails_before_any_socket_or_process(self):
        with patch("tls_gateway.socket.create_connection") as connect, patch("tls_gateway.subprocess.Popen") as spawn:
            with self.assertRaises(ssl.SSLCertVerificationError):
                check(replace(self.config, ca_file=None))
            connect.assert_not_called()
            spawn.assert_not_called()

    def test_wrong_certificate_hostname_fails_before_any_socket_or_process(self):
        self.write_access("https://different.example.invalid:" + str(self.gateway_port))
        with patch("tls_gateway.socket.create_connection") as connect, patch("tls_gateway.subprocess.Popen") as spawn:
            with self.assertRaises(ssl.SSLCertVerificationError):
                check(self.config)
            connect.assert_not_called()
            spawn.assert_not_called()

    def test_occupied_listener_is_not_mistaken_for_own_ready_gateway(self):
        with running_gateway(self.config, log_output=self.gateway_logs) as original:
            self.assertEqual(200, self.request()[0])
            with self.assertRaises((ValueError, RuntimeError, OSError)):
                with running_gateway(self.config, log_output=self.gateway_logs):
                    self.fail("A second gateway must not report the first gateway as its own")
            self.assertIsNone(original.poll())
            self.assertEqual(200, self.request()[0])

    def test_cli_sigterm_stops_child_and_closes_listener(self):
        output_path = self.root / "cli.log"
        command = [sys.executable, "-B", str(Path(__file__).with_name("tls_gateway.py")), "run",
                   "--access-config", str(self.access_path), "--credential-file", str(self.credential_path),
                   "--cert-file", str(self.cert), "--key-file", str(self.key), "--ca-file", str(self.ca),
                   "--backend-port", str(self.server.server_port), "--listen-port", str(self.gateway_port),
                   "--caddy", CADDY]
        with output_path.open("wb") as logs:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=logs, stderr=logs,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 12
                while "Gateway ready:" not in output_path.read_text():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail("CLI did not become ready: " + output_path.read_text())
                    time.sleep(0.05)
                self.assertEqual(200, self.request()[0])
                children = []
                if shutil.which("pgrep"):
                    result = subprocess.run([shutil.which("pgrep"), "-P", str(process.pid)],
                                            capture_output=True, text=True, timeout=3, check=True)
                    children = [int(pid) for pid in result.stdout.split()]
                    self.assertTrue(children)
                process.terminate()
                self.assertEqual(0, process.wait(timeout=12))
                with self.assertRaises(OSError):
                    with socket.create_connection(("127.0.0.1", self.gateway_port), timeout=1):
                        self.fail("TLS listener survived CLI termination")
                for pid in children:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)

    def test_chunked_post_is_rejected_before_backend(self):
        with running_gateway(self.config, log_output=self.gateway_logs):
            with patch.object(self.server.store, "receive_decision") as operation:
                status, _, _ = self.request("/api/v1/decisions", method="POST",
                    body=b"2\r\n{}\r\n0\r\n\r\n",
                    headers={"Content-Length": None, "Transfer-Encoding": "chunked"})
                self.assertIn(status, (400, 411))
                operation.assert_not_called()

    def test_proxy_does_not_retry_post_after_backend_loses_response(self):
        calls = []
        original = Handler.v1_command

        def disconnect(handler, path):
            if path != "/api/v1/decisions":
                return original(handler, path)
            calls.append(handler.rfile.read(int(handler.headers["Content-Length"])))
            handler.close_connection = True
            handler.connection.shutdown(socket.SHUT_RDWR)

        log_path = self.root / "gateway.log"
        with log_path.open("wb") as logs:
            with running_gateway(self.config, log_output=logs) as process:
                with patch.object(Handler, "v1_command", disconnect):
                    status, _, _ = self.request("/api/v1/decisions?secret=fictional-query-secret",
                                                method="POST", body=b'{"fictional":"body-secret"}')
                self.assertEqual(502, status)
                self.assertEqual([b'{"fictional":"body-secret"}'], calls)
            self.assertIsNotNone(process.poll())
        output = log_path.read_text()
        self.assertNotIn(self.token, output)
        self.assertNotIn(self.server.token, output)
        self.assertNotIn("fictional-query-secret", output)
        self.assertNotIn("body-secret", output)


if __name__ == "__main__":
    unittest.main()
