"""Gateway configuration and probe failures; no Caddy or sockets required."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from device_access import create_credentials
from tls_gateway import (GatewayConfig, MAX_PROBE_BYTES, _caddy_environment,
                         _read_probe, prepare)


class GatewayConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.access, self.token = create_credentials("https://jobs.example.invalid", self.root / "access.json",
                                                     self.root / "token.txt")
        self.config = GatewayConfig(self.access, self.token, self.root / "cert.pem", self.root / "key.pem")

    def test_rejects_invalid_ports_and_binding_before_opening_certificates(self):
        for changes in ({"backend_port": 0}, {"backend_port": 65536}, {"listen_port": -1},
                        {"listen_port": True}, {"listen_port": 8766},
                        {"listen_address": "some-host"}, {"listen_address": "::1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                prepare(replace(self.config, **changes))

    def test_rejects_public_origin_that_local_backend_would_accept(self):
        for host in ("127.0.0.1", "localhost"):
            raw = json.loads(self.access.read_text())
            raw["public_origin"] = f"https://{host}:8766"
            self.access.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "local Host aliases"):
                prepare(replace(self.config, listen_port=8443))

    def test_rejects_credential_symlink_or_loose_permissions(self):
        link = self.root / "token-link"
        link.symlink_to(self.token)
        with self.assertRaises(OSError):
            prepare(replace(self.config, credential_file=link))
        self.token.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            prepare(self.config)

    def test_ip_origin_is_rejected_before_certificate_or_listener_setup(self):
        for host in ("127.0.0.1", "[::1]", "192.0.2.1"):
            raw = json.loads(self.access.read_text())
            raw["public_origin"] = f"https://{host}:8443"
            self.access.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "DNS hostname"):
                prepare(self.config)

    def test_rejects_invalid_credential_without_including_secret_in_error(self):
        for value in ("x" * 43, "x" * 200, "Bearer " + "x" * 43, "non-ascii-\u00e9"):
            self.token.write_text(value)
            with self.assertRaises(ValueError) as caught:
                prepare(self.config)
            self.assertNotIn(value, str(caught.exception))

    def test_certificate_path_cannot_inject_caddy_placeholders(self):
        for name in ("{$SECRET}", "{env.SECRET}", "cert\nname"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "paths"):
                prepare(replace(self.config, cert_file=self.root / name))

    def test_caddy_state_is_isolated_and_removed_without_changing_parent_environment(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid", "https_proxy": "http://proxy.invalid"}):
            with _caddy_environment() as env:
                data_root = Path(env["XDG_DATA_HOME"]).parent
                self.assertTrue(data_root.is_dir())
                self.assertEqual(0o700, data_root.stat().st_mode & 0o777)
                self.assertNotIn("HTTP_PROXY", env)
                self.assertNotIn("https_proxy", env)
                self.assertIn("HTTP_PROXY", os.environ)
            self.assertFalse(data_root.exists())


class GatewayProbeTests(unittest.TestCase):
    def setUp(self):
        self.prepared = SimpleNamespace(access=SimpleNamespace(public_host="jobs.example.invalid"),
                                        credential="fictional-token", probe_id="own-listener")

    def probe(self, *, status=200, body=None, authenticated=True, own_listener=False, nonce=None):
        response = Mock(status=status)
        response.read.return_value = body if body is not None else b'{"schema_version":1,"capabilities":{"remote_access":true}}'
        response.getheader.return_value = nonce
        client = Mock()
        client.getresponse.return_value = response
        try:
            return _read_probe(client, self.prepared, authenticated=authenticated, own_listener=own_listener)
        finally:
            client.close.assert_called_once()
            response.read.assert_called_once_with(MAX_PROBE_BYTES + 1)

    def test_unauthenticated_backend_requires_401_and_never_follows_redirect(self):
        self.probe(status=401, authenticated=False)
        for code in (200, 301, 302, 403, 500):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.probe(status=code, authenticated=False)

    def test_authenticated_probe_rejects_redirects_errors_and_bad_capabilities(self):
        for code in (301, 302, 401, 403, 500):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.probe(status=code)
        for body in (b'invalid JSON', b'[]', b'{}', b'x' * (MAX_PROBE_BYTES + 1),
                     b'{"schema_version":true,"capabilities":{"remote_access":true}}',
                     b'{"schema_version":1,"capabilities":{"remote_access":false}}',
                     b'{"schema_version":1,"capabilities":{"remote_access":"true"}}'):
            with self.subTest(body=body[:80]), self.assertRaises(ValueError):
                self.probe(body=body)
        self.probe()

    def test_existing_listener_cannot_satisfy_new_gateway_probe(self):
        for nonce in (None, "other-instance"):
            with self.assertRaisesRegex(ValueError, "not the gateway"):
                self.probe(own_listener=True, nonce=nonce)
        self.probe(own_listener=True, nonce="own-listener")


if __name__ == "__main__":
    unittest.main()
