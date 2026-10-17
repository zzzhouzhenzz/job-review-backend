#!/usr/bin/env python3
"""Validate and run a dedicated Caddy TLS gateway for the loopback review API.

Certificates are supplied by the operator. This module does not obtain or renew
certificates, change networking, install trust roots, or start the backend.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import Message
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from device_access import DeviceAccess


READ_PATHS = ("/api/v1/capabilities", "/api/v1/health", "/api/v1/feed",
              "/api/v1/processes", "/api/v1/questions", "/api/v1/tasks",
              "/api/state", "/api/export")
WRITE_PATHS = ("/api/v1/decisions", "/api/v1/answers", "/api/v1/tasks/cancel",
               "/api/job", "/api/company", "/api/research", "/api/card",
               "/api/queue", "/api/cancel")
MAX_PROBE_BYTES = 64 * 1024


@dataclass(frozen=True)
class GatewayConfig:
    access_config: Path
    credential_file: Path
    cert_file: Path
    key_file: Path
    backend_port: int = 8766
    listen_address: str = "127.0.0.1"
    listen_port: int | None = None
    caddy: str = "caddy"
    ca_file: Path | None = None


@dataclass(frozen=True)
class PreparedGateway:
    config: GatewayConfig
    access: DeviceAccess
    listen_port: int
    hostname: str
    caddy: str
    caddyfile: str
    client_context: ssl.SSLContext = field(repr=False)
    credential: str = field(repr=False)
    probe_id: str = field(repr=False)


def _private_read(path, limit):
    """Do not follow a symlink or open a device when reading a secret."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("Secret files must be regular files owned by this user with mode 0600")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            result = source.read(limit + 1)
        if len(result) > limit:
            raise ValueError("Secret file exceeds its size limit")
        return result
    finally:
        os.close(descriptor)


def _quoted_path(path):
    path = str(Path(path).expanduser().absolute())
    # Caddy expands placeholders even inside quoted tokens. Do not interpret
    # names from the filesystem as Caddy configuration or environment values.
    if any(c in path for c in "{}\r\n\x00"):
        raise ValueError("Certificate paths must not contain braces or control characters")
    return json.dumps(path, ensure_ascii=False)


def _verify_certificate(server_context, client_context, hostname):
    """Verify trust, validity and hostname before opening any network listener."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    peer_in, peer_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(incoming, outgoing, server_hostname=hostname)
    server = server_context.wrap_bio(peer_in, peer_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        if outgoing.pending:
            peer_in.write(outgoing.read())
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        if peer_out.pending:
            incoming.write(peer_out.read())
        if client_done and server_done:
            return
    raise ValueError("Certificate verification handshake did not complete")


def prepare(config):
    """Validate local inputs and build a secret-free, fixed-upstream Caddyfile."""
    access = DeviceAccess.from_file(Path(config.access_config).expanduser())
    hostname = urlsplit(access.public_origin).hostname
    listen_port = config.listen_port if config.listen_port is not None else (urlsplit(access.public_origin).port or 443)
    for port in (config.backend_port, listen_port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Gateway and backend ports must be between 1 and 65535")
    address = ipaddress.ip_address(config.listen_address)
    if address.version != 4:
        raise ValueError("This gateway version requires an IPv4 listen address")
    if listen_port == config.backend_port:
        raise ValueError("Gateway and backend must use different ports")
    if access.public_host in (f"127.0.0.1:{config.backend_port}", f"localhost:{config.backend_port}"):
        raise ValueError("Public origin must differ from the backend's local Host aliases")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        # TLS clients omit SNI for literal IP addresses, which is incompatible
        # with the gateway's strict SNI/Host check. Reject before launching.
        raise ValueError("The TLS gateway requires a DNS hostname, not a literal IP address")
    try:
        credential = _private_read(Path(config.credential_file).expanduser(), 128).decode("ascii").removesuffix("\n")
    except UnicodeError:
        raise ValueError("Credential file must contain the provisioned ASCII token") from None
    headers = Message()
    headers["Authorization"] = "Bearer " + credential
    if not access.authenticate(headers):
        raise ValueError("Credential file does not match the device access configuration")
    cert_path, key_path = _quoted_path(config.cert_file), _quoted_path(config.key_file)
    _private_read(Path(config.key_file).expanduser(), 1024 * 1024)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    # An empty password prevents OpenSSL from prompting for an encrypted key.
    server_context.load_cert_chain(json.loads(cert_path), json.loads(key_path), password="")
    client_context = ssl.create_default_context()
    client_context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config.ca_file is not None:
        client_context.load_verify_locations(cafile=str(Path(config.ca_file).expanduser()))
    _verify_certificate(server_context, client_context, hostname)
    caddy = shutil.which(str(config.caddy))
    if caddy is None:
        raise ValueError("Caddy executable not found; install Caddy or supply --caddy PATH")
    probe_id = secrets.token_hex(16)
    read_paths = ", ".join(repr(p) for p in READ_PATHS)
    write_paths = ", ".join(repr(p) for p in WRITE_PATHS)
    caddyfile = f"""{{
    admin off
    persist_config off
    auto_https off
    skip_install_trust
    grace_period 5s
    log default {{
        format filter {{
            wrap json
            fields {{
                request delete
                resp_headers delete
            }}
        }}
    }}
    servers {{
        protocols h1 h2
        strict_sni_host on
        max_header_size 16KB
        timeouts {{
            read_header 5s
            read_body 15s
            write 35s
            idle 30s
        }}
    }}
}}
https://:{listen_port} {{
    bind {address}
    tls {cert_path} {key_path} {{
        protocols tls1.2 tls1.3
    }}
    @wrong_authority expression `{{http.request.hostport}} != "{access.public_host}"`
    @unframed {{
        method POST
        not header Content-Length *
    }}
    @oversized expression `{{http.request.header.Content-Length}} != "" && int({{http.request.header.Content-Length}}) > 1000000`
    @api expression `(method('GET') && path({read_paths})) || (method('POST') && path({write_paths}))`
    @probe {{
        path /api/v1/capabilities
        header X-Job-Review-Probe {probe_id}
    }}
    route {{
        header Cache-Control no-store
        header X-Content-Type-Options nosniff
        respond @wrong_authority "Invalid authority." 421 {{
            close
        }}
        respond @unframed "A Content-Length is required." 411 {{
            close
        }}
        respond @oversized "Request body is too large." 413 {{
            close
        }}
        request_body {{
            max_size 1000000
        }}
        header @probe X-Job-Review-Probe {probe_id}
        reverse_proxy @api 127.0.0.1:{config.backend_port} {{
            lb_retries 0
            lb_try_duration 0s
            transport http {{
                versions 1.1
                keepalive off
                max_conns_per_host 16
                dial_timeout 3s
                response_header_timeout 25s
            }}
        }}
        respond "API route not found." 404 {{
            close
        }}
    }}
}}
"""
    return PreparedGateway(config, access, listen_port, hostname, caddy, caddyfile,
                           client_context, credential, probe_id)


def _read_probe(client, prepared, *, authenticated, own_listener=False):
    headers = {"Host": prepared.access.public_host, "Connection": "close"}
    if authenticated:
        headers["Authorization"] = "Bearer " + prepared.credential
    if own_listener:
        headers["X-Job-Review-Probe"] = prepared.probe_id
    try:
        client.request("GET", "/api/v1/capabilities", headers=headers)
        response = client.getresponse()
        body = response.read(MAX_PROBE_BYTES + 1)
        if len(body) > MAX_PROBE_BYTES:
            raise ValueError("Backend capabilities response exceeds the size limit")
        if own_listener and response.getheader("X-Job-Review-Probe") != prepared.probe_id:
            raise ValueError("TLS listener is not the gateway started by this process")
        if not authenticated:
            if response.status != 401:
                raise ValueError("Backend must reject unauthenticated capabilities with HTTP 401")
            return
        if response.status != 200:
            raise ValueError(f"Authenticated backend capabilities returned HTTP {response.status}")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Backend capabilities response is not valid JSON") from None
        if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1
                or not isinstance(payload.get("capabilities"), dict)
                or payload["capabilities"].get("remote_access") is not True):
            raise ValueError("Backend does not report authenticated device access")
    finally:
        client.close()


def _backend_check(prepared):
    for authenticated in (False, True):
        client = http.client.HTTPConnection("127.0.0.1", prepared.config.backend_port, timeout=3)
        _read_probe(client, prepared, authenticated=authenticated)


@contextmanager
def _caddy_environment():
    # Neither validation nor startup writes to the user's other Caddy instance.
    with tempfile.TemporaryDirectory(prefix="job-review-tls-") as directory:
        env = {**os.environ, "XDG_DATA_HOME": directory + "/data", "XDG_CONFIG_HOME": directory + "/config"}
        # Outbound requests are not needed for supplied certificates. In
        # particular, a shell proxy must never redirect the loopback upstream.
        for key in tuple(env):
            if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                env.pop(key)
        yield env


def _validate(prepared, env):
    result = subprocess.run([prepared.caddy, "validate", "--config", "-", "--adapter", "caddyfile"],
                            input=prepared.caddyfile, text=True, capture_output=True, env=env, timeout=15)
    if result.returncode:
        raise ValueError("Caddy configuration validation failed:\n" + result.stderr[-8000:])


def check(config):
    """Validate certificates, Caddy configuration and the live backend without binding TLS."""
    prepared = prepare(config)
    _backend_check(prepared)
    with _caddy_environment() as env:
        _validate(prepared, env)
    return prepared


def _tls_probe(prepared):
    address = prepared.config.listen_address
    if address == "0.0.0.0":
        address = "127.0.0.1"
    client = http.client.HTTPConnection(address, prepared.listen_port, timeout=1)
    raw = socket.create_connection((address, prepared.listen_port), timeout=1)
    try:
        client.sock = prepared.client_context.wrap_socket(raw, server_hostname=prepared.hostname)
    except BaseException:
        raw.close()
        raise
    _read_probe(client, prepared, authenticated=True, own_listener=True)


@contextmanager
def running_gateway(config, *, log_output=None):
    """Yield the foreground Caddy process only after a verified HTTPS probe passes."""
    prepared = prepare(config)
    _backend_check(prepared)
    with _caddy_environment() as env:
        _validate(prepared, env)
        process = subprocess.Popen([prepared.caddy, "run", "--config", "-", "--adapter", "caddyfile"],
                                   stdin=subprocess.PIPE, stdout=log_output, stderr=log_output,
                                   text=True, env=env, start_new_session=True)
        try:
            process.stdin.write(prepared.caddyfile)
            process.stdin.close()
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Caddy exited during startup (status {process.returncode}); see gateway diagnostics")
                try:
                    _tls_probe(prepared)
                    break
                except (OSError, http.client.HTTPException):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Gateway did not pass its HTTPS readiness probe within 10 seconds") from None
                    time.sleep(0.1)
            yield process
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "run"))
    for name in ("access-config", "credential-file", "cert-file", "key-file"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--backend-port", type=int, default=8766)
    parser.add_argument("--listen-address", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int)
    parser.add_argument("--caddy", default="caddy")
    parser.add_argument("--ca-file", type=Path, help="Additional trusted CA for gateway checks; never disables certificate validation")
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    config = GatewayConfig(**args)

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        if command == "check":
            prepared = check(config)
            print(f"Gateway preflight passed for {prepared.access.public_origin}. No TLS listener was started.")
        else:
            with running_gateway(config) as process:
                print("Gateway ready: certificate and authenticated HTTPS probe passed.", flush=True)
                status = process.wait()
                if status:
                    raise RuntimeError(f"Caddy exited with status {status}; see gateway diagnostics")
    except KeyboardInterrupt:
        print("Gateway stopped.", flush=True)
    except (ValueError, OSError, RuntimeError, http.client.HTTPException, subprocess.SubprocessError) as error:
        parser.exit(1, f"Gateway error: {error}\n")
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
