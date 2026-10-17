"""Device credentials remain confined to temporary, private test files."""
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import device_access
from device_access import DeviceAccess, create_credentials, main, normalize_origin


TOKEN = "a" * 43


class DeviceAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "device.json"
        self.credential = self.root / "credential.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, **updates):
        config = {"schema_version": 1, "public_origin": "https://jobs.example.test",
                  "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest()}
        config.update(updates)
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o600)
        return config

    def headers(self, *values):
        result = Message()
        for value in values:
            result["Authorization"] = value
        return result

    def test_origin_normalizes_dns_default_port_and_trailing_slash(self):
        self.assertEqual("https://jobs.example.test", normalize_origin("HTTPS://Jobs.Example.Test:443/"))
        self.assertEqual("https://jobs.example.test:8443", normalize_origin("https://jobs.example.test:8443/"))
        self.assertEqual("https://xn--bcher-kva.example", normalize_origin("https://xn--bcher-kva.example"))

    def test_origin_accepts_ip_and_formats_ipv6_host(self):
        self.assertEqual("https://127.0.0.1", normalize_origin("https://127.0.0.1:443/"))
        self.write_config(public_origin="https://[2001:0DB8:0:0:0:0:0:1]:8443/")
        access = DeviceAccess.from_file(self.config)
        self.assertEqual("https://[2001:db8::1]:8443", access.public_origin)
        self.assertEqual("[2001:db8::1]:8443", access.public_host)

    def test_rejects_ambiguous_or_non_https_origins(self):
        for origin in (None, True, 1, "", "http://jobs.example.test", "https:///jobs.example.test",
                       "https://name:password@jobs.example.test", "https://@jobs.example.test",
                       "https://jobs.example.test/path", "https://jobs.example.test//",
                       "https://jobs.example.test?", "https://jobs.example.test#",
                       "https://jobs.example.test/?a=b", "https://jobs.example.test/#a",
                       " https://jobs.example.test", "https://jobs.example.test\n",
                       "https://jobs.exa\tmple.test", "https://jobs.example.test\\evil",
                       "https://jobs.example.test:", "https://jobs.example.test:0",
                       "https://jobs.example.test:65536", "https://jobs.example.test:abc",
                       "https://jobs.example.test:+443", "https://jobs.example.test.",
                       "https://bad_name.example", "https://-bad.example", "https://bad-.example",
                       "https://jobs..example", "https://" + "a" * 64 + ".example",
                       "https://999.2.3.4", "https://0127.0.0.1", "https://12345",
                       "https://bücher.example", "https://jobs%2eexample.test",
                       "https://[fe80::1%25en0]", "https://[::1]evil", "https://[::1]:",
                       "https://[::1", "https://2001:db8::1", "https://jobs.example.test\x7f"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                normalize_origin(origin)

    def test_private_config_loads_without_exposing_digest_or_token_in_repr(self):
        config = self.write_config(public_origin="https://JOBS.EXAMPLE.TEST:443/")
        access = DeviceAccess.from_file(self.config)
        self.assertEqual("https://jobs.example.test", access.public_origin)
        self.assertEqual("jobs.example.test", access.public_host)
        self.assertNotIn(config["token_sha256"], repr(access))
        self.assertNotIn(TOKEN, repr(access))

    def test_rejects_non_private_permissions(self):
        self.write_config()
        for mode in (0o644, 0o640, 0o400, 0o700, 0o660, 0o1600):
            with self.subTest(mode=mode):
                self.config.chmod(mode)
                with self.assertRaisesRegex(ValueError, "mode 0600"):
                    DeviceAccess.from_file(self.config)

    def test_rejects_wrong_owner(self):
        self.write_config()
        with patch("device_access.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(ValueError, "current user"):
                DeviceAccess.from_file(self.config)

    def test_rejects_symlink_and_non_regular_file(self):
        self.write_config()
        link = self.root / "linked.json"
        link.symlink_to(self.config)
        with self.assertRaises(OSError):
            DeviceAccess.from_file(link)
        fifo = self.root / "pipe"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ValueError, "regular file"):
            DeviceAccess.from_file(fifo)

    def test_missing_config_fails(self):
        with self.assertRaises(FileNotFoundError):
            DeviceAccess.from_file(self.config)

    def test_malformed_json_is_rejected_without_quoting_contents(self):
        for payload in (b"not-json-private", b"\xff", b"[]", b"{}", b"null",
                        b'{"schema_version":1,"schema_version":1}',
                        b"[" * 2000 + b"]" * 2000, b" " * (16 * 1024 + 1)):
            with self.subTest(payload=payload[:40]):
                self.config.write_bytes(payload)
                self.config.chmod(0o600)
                with self.assertRaises(ValueError) as failure:
                    DeviceAccess.from_file(self.config)
                self.assertNotIn("not-json-private", str(failure.exception))

    def test_invalid_config_fields_are_rejected(self):
        for updates in ({"schema_version": True}, {"schema_version": "1"}, {"schema_version": 2},
                        {"token_sha256": "A" * 64}, {"token_sha256": "a" * 63},
                        {"token_sha256": 5}, {"public_origin": "http://example.test"},
                        {"raw_token": TOKEN}):
            with self.subTest(updates=updates):
                self.write_config(**updates)
                with self.assertRaises(ValueError):
                    DeviceAccess.from_file(self.config)

    def test_exact_bearer_header_authenticates_case_insensitive_scheme(self):
        self.write_config()
        access = DeviceAccess.from_file(self.config)
        for scheme in ("Bearer", "bearer", "BEARER", "bEaReR"):
            self.assertTrue(access.authenticate(self.headers(scheme + " " + TOKEN)))
        headers = Message()
        headers["authorization"] = "Bearer " + TOKEN
        self.assertTrue(access.authenticate(headers))

    def test_missing_wrong_duplicate_or_malformed_authorization_fails(self):
        self.write_config()
        access = DeviceAccess.from_file(self.config)
        invalid = ((), ("Bearer " + "b" * 43,), ("Bearer " + TOKEN, "Bearer " + TOKEN),
                   ("Basic " + TOKEN,), (" Bearer " + TOKEN,), ("Bearer  " + TOKEN,),
                   ("Bearer\t" + TOKEN,), ("Bearer " + TOKEN + " ",),
                   ("Bearer " + TOKEN + "\n",), ("Bearer " + TOKEN + "=",),
                   ("Bearer " + TOKEN[:-1],), ("Bearer " + "a" * 42 + "/",),
                   ("Bearer " + "a" * 42 + "é",), ("Bearer " + TOKEN + ",Bearer " + TOKEN,))
        for values in invalid:
            with self.subTest(values=values):
                self.assertFalse(access.authenticate(self.headers(*values)))

    def test_provision_creates_private_files_and_verifiable_32_byte_token(self):
        create_credentials("HTTPS://JOBS.EXAMPLE.TEST:443/", self.config, self.credential)
        access = DeviceAccess.from_file(self.config)
        token = self.credential.read_text().strip()
        self.assertRegex(token, r"\A[A-Za-z0-9_-]{43}\Z")
        self.assertTrue(access.authenticate(self.headers("Bearer " + token)))
        self.assertNotIn(token, self.config.read_text())
        for path in (self.config, self.credential):
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(os.geteuid(), path.stat().st_uid)

    def test_provision_and_cli_never_print_token(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            main(["create", "--origin", "https://jobs.example.test", "--config", str(self.config),
                  "--credential-output", str(self.credential)])
        token = self.credential.read_text().strip()
        self.assertNotIn(token, output.getvalue() + errors.getvalue())
        self.assertIn(str(self.config), output.getvalue())
        self.assertIn(str(self.credential), output.getvalue())
        self.assertEqual("", errors.getvalue())

    def test_validation_precedes_writes(self):
        for origin, config, credential in (
                ("http://jobs.example.test", self.config, self.credential),
                ("https://jobs.example.test", self.config, self.config),
                ("https://jobs.example.test", self.config, self.root / "missing" / "token")):
            with self.subTest(origin=origin, config=config, credential=credential):
                with self.assertRaises(ValueError):
                    create_credentials(origin, config, credential)
                self.assertEqual([], list(self.root.iterdir()))

    def test_preexisting_outputs_are_not_overwritten_or_removed(self):
        for existing, other in ((self.config, self.credential), (self.credential, self.config)):
            with self.subTest(existing=existing):
                existing.write_text("existing content")
                with self.assertRaises(FileExistsError):
                    create_credentials("https://jobs.example.test", self.config, self.credential)
                self.assertEqual("existing content", existing.read_text())
                self.assertFalse(other.exists())
                existing.unlink()

    def test_dangling_output_symlink_is_not_followed(self):
        destination = self.root / "other"
        self.credential.symlink_to(destination)
        with self.assertRaises(FileExistsError):
            create_credentials("https://jobs.example.test", self.config, self.credential)
        self.assertFalse(self.config.exists())
        self.assertFalse(destination.exists())
        self.assertTrue(self.credential.is_symlink())

    def test_partial_failure_cleans_only_files_created_by_this_call(self):
        real_write = device_access._private_write

        def collide(path, payload, created):
            if path == self.credential:
                path.write_text("another writer")
            return real_write(path, payload, created)

        with patch("device_access._private_write", side_effect=collide):
            with self.assertRaises(FileExistsError):
                create_credentials("https://jobs.example.test", self.config, self.credential)
        self.assertFalse(self.config.exists())
        self.assertEqual("another writer", self.credential.read_text())

    def test_write_failure_cleans_both_outputs(self):
        real_sync = os.fsync
        calls = []

        def fail_second(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("Fictional disk failure")
            return real_sync(descriptor)

        with patch("device_access.os.fsync", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "Fictional disk failure"):
                create_credentials("https://jobs.example.test", self.config, self.credential)
        self.assertEqual([], list(self.root.iterdir()))

    def test_cleanup_preserves_replaced_config(self):
        real_write = device_access._private_write

        def replace_then_fail(path, payload, created):
            if path == self.credential:
                replacement = self.root / "replacement"
                replacement.write_text("replacement content")
                replacement.replace(self.config)
                raise OSError("Fictional write failure")
            return real_write(path, payload, created)

        with patch("device_access._private_write", side_effect=replace_then_fail):
            with self.assertRaises(OSError):
                create_credentials("https://jobs.example.test", self.config, self.credential)
        self.assertEqual("replacement content", self.config.read_text())
        self.assertFalse(self.credential.exists())


if __name__ == "__main__":
    unittest.main()
