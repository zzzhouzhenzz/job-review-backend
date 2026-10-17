#!/usr/bin/env python3
"""Provision a private bearer credential for a separately secured HTTPS gateway.

The public origin is an allowlist value. It does not enable TLS, prove that a
gateway exists, or authorize forwarded headers.
"""
import argparse
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import urlsplit


_DIGEST = re.compile(r"[0-9a-f]{64}")
_BEARER = re.compile(r"(?i:Bearer) ([A-Za-z0-9_-]{43})")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CONFIG_KEYS = {"schema_version", "public_origin", "token_sha256"}
_MAX_CONFIG_BYTES = 16 * 1024


def normalize_origin(value):
    """Return an HTTPS origin with a lowercase host and no default port."""
    if (not isinstance(value, str) or not value or not value.isascii()
            or any(character.isspace() or ord(character) < 32
                   or ord(character) == 127 for character in value)
            or any(character in value for character in "\\?#")):
        raise ValueError("public_origin must be an ASCII HTTPS origin without a query or fragment")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("public_origin contains an invalid host or port") from None
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.netloc.endswith(":")):
        raise ValueError("public_origin must use HTTPS and contain only a host and optional port")
    authority_pattern = (r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?" if parsed.netloc.startswith("[")
                         else r"[a-zA-Z0-9.-]+(?::[0-9]+)?")
    if not re.fullmatch(authority_pattern, parsed.netloc):
        raise ValueError("public_origin contains an invalid host or port")
    host = host.lower()
    if "%" in host:
        raise ValueError("public_origin must not contain a scoped or encoded host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (":" in host or len(host) > 253
                or all(character in "0123456789." for character in host)
                or not all(_DNS_LABEL.fullmatch(label) for label in host.split("."))):
            raise ValueError("public_origin must contain a valid DNS name or IP address") from None
        normalized_host = host
    else:
        normalized_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("public_origin port must be between 1 and 65535")
    suffix = f":{port}" if port not in (None, 443) else ""
    return f"https://{normalized_host}{suffix}"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Device access config contains a duplicate field")
        result[key] = value
    return result


@dataclass(frozen=True)
class DeviceAccess:
    public_origin: str
    _token_sha256: str = field(repr=False)

    @property
    def public_host(self):
        return self.public_origin[len("https://"):]

    @classmethod
    def from_file(cls, path):
        """Read a regular, owner-only POSIX config without following a symlink."""
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("Device access config requires POSIX private-file support")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Device access config must be a regular file")
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Device access config must belong to the current user with mode 0600")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                raw = source.read(_MAX_CONFIG_BYTES + 1)
            if len(raw) > _MAX_CONFIG_BYTES:
                raise ValueError("Device access config exceeds the size limit")
        finally:
            os.close(descriptor)
        try:
            config = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("Device access config must contain valid UTF-8 JSON") from None
        if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
            raise ValueError("Device access config must contain schema_version, public_origin, and token_sha256")
        if type(config["schema_version"]) is not int or config["schema_version"] != 1:
            raise ValueError("Device access config schema_version must be 1")
        token_hash = config["token_sha256"]
        if not isinstance(token_hash, str) or not _DIGEST.fullmatch(token_hash):
            raise ValueError("Device access config token_sha256 must be 64 lowercase hexadecimal characters")
        return cls(normalize_origin(config["public_origin"]), token_hash)

    def authenticate(self, headers):
        """Accept exactly one Authorization field containing a 32-byte bearer token."""
        values = headers.get_all("Authorization")
        if not values or len(values) != 1 or not isinstance(values[0], str):
            return False
        match = _BEARER.fullmatch(values[0])
        if match is None:
            return False
        supplied_hash = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
        return secrets.compare_digest(supplied_hash, self._token_sha256)


def _private_write(path, payload, created):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        created.append((path, info.st_dev, info.st_ino))
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_credentials(origin, config_path, credential_output):
    """Create a config and separate token file, with no overwrite or secret output."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("Device access provisioning requires POSIX private-file support")
    origin = normalize_origin(origin)
    paths = [Path(config_path).expanduser().absolute(),
             Path(credential_output).expanduser().absolute()]
    if paths[0].resolve() == paths[1].resolve():
        raise ValueError("Config and credential output must be different files")
    for path in paths:
        if not path.parent.is_dir():
            raise ValueError("Each output file must have an existing parent directory")
        if os.path.lexists(path):
            raise FileExistsError(f"Output already exists: {path}")
    token = secrets.token_urlsafe(32)
    config = {"schema_version": 1, "public_origin": origin,
              "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest()}
    payloads = [(json.dumps(config, indent=2) + "\n").encode("utf-8"),
                (token + "\n").encode("ascii")]
    created = []
    try:
        for path, payload in zip(paths, payloads):
            _private_write(path, payload, created)
    except BaseException:
        # Do not remove a pre-existing file or a file replaced by another writer.
        for path, device, inode in reversed(created):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (info.st_dev, info.st_ino) == (device, inode):
                path.unlink()
        raise
    return tuple(paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create a private config and separate device credential")
    create.add_argument("--origin", required=True, help="Public HTTPS origin provided by your gateway")
    create.add_argument("--config", required=True, type=Path)
    create.add_argument("--credential-output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config_path, credential_path = create_credentials(args.origin, args.config, args.credential_output)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"Device access config: {config_path}")
    print(f"Device credential file: {credential_path}")
    print("Transfer the credential privately to the iPhone. Do not share it in logs or chat.")
    print("Configure HTTPS on the gateway before enabling remote access.")


if __name__ == "__main__":
    main()
