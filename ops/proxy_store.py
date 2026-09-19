"""Private proxy inventory with credentials kept outside metadata.

The registry intentionally stores only operational metadata and a digest of the
upstream session identity.  Authentication material lives in one chmod-0600
JSON file per proxy.  Public methods return redacted copies and never return a
password or a secret path.
"""
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .secureio import atomic_json, read_private_json, require_private_directory
from .device_ids import canonical_device


SCHEMA_VERSION = 1
PROXY_ID_RE = re.compile(r"[a-z][a-z0-9-]{2,62}")
_HEALTH_URL = "https://api.ipify.org"


def _proxy_id(value):
    if not isinstance(value, str) or not PROXY_ID_RE.fullmatch(value):
        raise ValueError("proxy id must be 3-63 lowercase letters, digits or hyphens, starting with a letter")
    return value


def _device_id(value):
    if not isinstance(value, str):
        raise ValueError("device id must be a canonical numNN identifier")
    try:
        canonical = canonical_device(value)
    except ValueError as exc:
        raise ValueError("device id must be a canonical numNN identifier") from exc
    if canonical != value:
        raise ValueError("device id must be a canonical numNN identifier")
    return value


def _text(value, name, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} may not contain control characters")
    return value


def _kind(value):
    if value == "socks5":
        return "socks"
    if value not in {"http", "socks"}:
        raise ValueError("proxy type must be http, socks or socks5")
    return value


def _public_ipv4(value, name):
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a pinned public IPv4 address") from exc
    if not address.is_global:
        raise ValueError(f"{name} must be a pinned public IPv4 address")
    return str(address)


def _port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("server_port must be an integer between 1 and 65535")
    return value


def _session_digest(kind, server, port, username):
    canonical = json.dumps([kind, server, port, username], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _username_hint(username):
    if len(username) <= 2:
        return "*" * len(username)
    return username[0] + "***" + username[-1]


def _curl_quote(value):
    """Quote a curl config value; secrets are sent through stdin, never argv."""
    _text(value, "curl configuration value", 4096)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def curl_health_config(secret, url=_HEALTH_URL):
    """Return stdin content for ``curl --config -`` without exposing argv secrets."""
    kind = _kind(secret.get("type"))
    server = _public_ipv4(secret.get("server"), "server")
    port = _port(secret.get("server_port"))
    username = _text(secret.get("username"), "username", 512)
    password = _text(secret.get("password"), "password", 4096)
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("health URL must use HTTPS")
    scheme = "socks5h" if kind == "socks" else "http"
    values = [
        "silent",
        "show-error",
        "fail",
        "connect-timeout = 10",
        "max-time = 20",
        'proto = "=https"',
        f"proxy = {_curl_quote(f'{scheme}://{server}:{port}')}",
        f"proxy-user = {_curl_quote(username + ':' + password)}",
        f"url = {_curl_quote(url)}",
    ]
    return "\n".join(values) + "\n"


class ProxyStore:
    """Root/private metadata registry and per-proxy credential store."""

    def __init__(self, registry=Path("/var/lib/android-farm/proxies.json"),
                 secret_dir=Path("/etc/android-farm/proxies")):
        self.registry = Path(registry)
        self.secret_dir = Path(secret_dir)

    def _prepare(self):
        require_private_directory(self.registry.parent, "proxy registry directory", create=True)
        require_private_directory(self.secret_dir, "proxy secret directory", create=True)

    @contextmanager
    def _lock(self):
        self._prepare()
        lock_path = self.registry.parent / ".proxies.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            if os.name == "posix":
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "posix":
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _empty():
        return {"schema_version": SCHEMA_VERSION, "proxies": {}}

    def _load(self):
        if not self.registry.exists():
            return self._empty()
        value = read_private_json(self.registry, "proxy registry")
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(f"proxy registry schema must be {SCHEMA_VERSION}")
        records = value.get("proxies")
        if not isinstance(records, dict):
            raise RuntimeError("proxy registry structure is invalid")
        sessions = set()
        assignments = set()
        for key, record in records.items():
            try:
                _proxy_id(key)
                if not isinstance(record, dict) or record.get("id") != key:
                    raise ValueError("record id differs from its key")
                _kind(record.get("type"))
                _public_ipv4(record.get("server"), "server")
                _public_ipv4(record.get("expected_egress_ip"), "expected_egress_ip")
                _port(record.get("server_port"))
                _text(record.get("label"), "label", 80)
                _text(record.get("username_hint"), "username_hint", 16)
                digest = record.get("session_sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("invalid session digest")
                if digest in sessions:
                    raise ValueError("duplicate endpoint/username session")
                sessions.add(digest)
                if record.get("state") not in {"enabled", "disabled"}:
                    raise ValueError("invalid proxy state")
                version = record.get("secret_version")
                if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                    raise ValueError("invalid secret version")
                assigned = record.get("assigned_device")
                if assigned is not None:
                    _device_id(assigned)
                    if assigned in assignments:
                        raise ValueError("device assigned to more than one proxy")
                    assignments.add(assigned)
            except ValueError as exc:
                raise RuntimeError(f"invalid proxy registry record {key!r}: {exc}") from exc
        return value

    def _save(self, value):
        atomic_json(self.registry, value)

    def _secret_path(self, proxy_id):
        return self.secret_dir / (_proxy_id(proxy_id) + ".json")

    def _read_secret(self, proxy_id, record):
        secret = read_private_json(self._secret_path(proxy_id), f"{proxy_id} proxy secret")
        required = {"type", "server", "server_port", "username", "password", "secret_version"}
        if not isinstance(secret, dict) or not required.issubset(secret):
            raise RuntimeError(f"{proxy_id} proxy secret structure is invalid")
        try:
            kind = _kind(secret["type"])
            server = _public_ipv4(secret["server"], "server")
            port = _port(secret["server_port"])
            username = _text(secret["username"], "username", 512)
            _text(secret["password"], "password", 4096)
        except ValueError as exc:
            raise RuntimeError(f"{proxy_id} proxy secret is invalid: {exc}") from exc
        if (kind != record["type"] or server != record["server"] or port != record["server_port"] or
                _session_digest(kind, server, port, username) != record["session_sha256"] or
                secret["secret_version"] != record["secret_version"]):
            raise RuntimeError(f"{proxy_id} proxy secret does not match registry metadata")
        return secret

    @staticmethod
    def _redacted(record):
        safe = {key: value for key, value in record.items() if key not in {"session_sha256"}}
        safe["credential"] = safe.pop("username_hint")
        return safe

    def add(self, proxy_id, *, label, proxy_type, server, server_port, username, password,
            expected_egress_ip):
        proxy_id = _proxy_id(proxy_id)
        label = _text(label, "label", 80)
        kind = _kind(proxy_type)
        server = _public_ipv4(server, "server")
        server_port = _port(server_port)
        username = _text(username, "username", 512)
        password = _text(password, "password", 4096)
        expected = _public_ipv4(expected_egress_ip, "expected_egress_ip")
        digest = _session_digest(kind, server, server_port, username)
        now = int(time.time())
        record = {"id": proxy_id, "label": label, "type": kind, "server": server,
                  "server_port": server_port, "expected_egress_ip": expected,
                  "username_hint": _username_hint(username), "session_sha256": digest,
                  "state": "enabled", "assigned_device": None, "secret_version": 1,
                  "created_at": now, "updated_at": now, "last_health": None}
        secret = {"type": kind, "server": server, "server_port": server_port,
                  "username": username, "password": password, "secret_version": 1}
        with self._lock():
            registry = self._load()
            if proxy_id in registry["proxies"]:
                raise RuntimeError("proxy id already exists")
            if any(item["session_sha256"] == digest for item in registry["proxies"].values()):
                raise RuntimeError("endpoint and username session already exists")
            path = self._secret_path(proxy_id)
            if path.exists() or path.is_symlink():
                raise RuntimeError("proxy secret path already exists")
            atomic_json(path, secret)
            try:
                registry["proxies"][proxy_id] = record
                self._save(registry)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return self._redacted(record)

    def list(self, include_disabled=True):
        with self._lock():
            records = self._load()["proxies"].values()
            return [self._redacted(record) for record in sorted(records, key=lambda item: item["id"])
                    if include_disabled or record["state"] == "enabled"]

    def show(self, proxy_id):
        proxy_id = _proxy_id(proxy_id)
        with self._lock():
            record = self._load()["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            return self._redacted(record)

    def provisioning_secret(self, proxy_id):
        """Return a copy for the trusted host provisioner; callers must never log it."""
        proxy_id = _proxy_id(proxy_id)
        with self._lock():
            record = self._load()["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            if record["state"] != "enabled":
                raise RuntimeError("disabled proxy cannot be provisioned")
            secret = dict(self._read_secret(proxy_id, record))
            secret.pop("secret_version", None)
            return secret

    def disable(self, proxy_id):
        proxy_id = _proxy_id(proxy_id)
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            record["state"] = "disabled"
            record["updated_at"] = int(time.time())
            self._save(registry)
            return self._redacted(record)

    def enable(self, proxy_id):
        proxy_id = _proxy_id(proxy_id)
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            record["state"] = "enabled"
            record["updated_at"] = int(time.time())
            self._save(registry)
            return self._redacted(record)

    def delete(self, proxy_id):
        proxy_id = _proxy_id(proxy_id)
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            if record["assigned_device"] is not None:
                raise RuntimeError("assigned proxy cannot be deleted")
            secret = self._read_secret(proxy_id, record)
            del registry["proxies"][proxy_id]
            self._save(registry)
            try:
                self._secret_path(proxy_id).unlink()
            except BaseException:
                registry["proxies"][proxy_id] = record
                self._save(registry)
                atomic_json(self._secret_path(proxy_id), secret)
                raise

    def assign(self, proxy_id, device_id):
        proxy_id, device_id = _proxy_id(proxy_id), _device_id(device_id)
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            if record["state"] != "enabled":
                raise RuntimeError("disabled proxy cannot be assigned")
            if record["assigned_device"] not in {None, device_id}:
                raise RuntimeError("proxy is already assigned to another device")
            for other in registry["proxies"].values():
                if other["id"] != proxy_id and other["assigned_device"] == device_id:
                    raise RuntimeError("device already has an assigned proxy")
            record["assigned_device"] = device_id
            record["updated_at"] = int(time.time())
            self._save(registry)
            return self._redacted(record)

    def unassign(self, proxy_id, device_id=None):
        proxy_id = _proxy_id(proxy_id)
        if device_id is not None:
            device_id = _device_id(device_id)
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            if device_id is not None and record["assigned_device"] != device_id:
                raise RuntimeError("proxy assignment does not match device")
            record["assigned_device"] = None
            record["updated_at"] = int(time.time())
            self._save(registry)
            return self._redacted(record)

    def rotate_password(self, proxy_id, password):
        proxy_id = _proxy_id(proxy_id)
        password = _text(password, "password", 4096)
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            old_secret = self._read_secret(proxy_id, record)
            new_secret = dict(old_secret, password=password,
                              secret_version=record["secret_version"] + 1)
            atomic_json(self._secret_path(proxy_id), new_secret)
            try:
                record["secret_version"] += 1
                record["updated_at"] = int(time.time())
                self._save(registry)
            except BaseException:
                atomic_json(self._secret_path(proxy_id), old_secret)
                raise
            return self._redacted(record)

    def check_health(self, proxy_id, *, runner=subprocess.run, timeout=30):
        """Check egress with curl config on stdin and persist only redacted evidence."""
        proxy_id = _proxy_id(proxy_id)
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None:
                raise KeyError("proxy does not exist")
            if record["state"] != "enabled":
                raise RuntimeError("disabled proxy cannot be health-checked")
            secret = self._read_secret(proxy_id, record)
            version = record["secret_version"]
            expected = record["expected_egress_ip"]
        checked_at = int(time.time())
        try:
            result = runner(["curl", "--config", "-"], input=curl_health_config(secret), text=True,
                            capture_output=True, timeout=timeout)
            if result.returncode != 0:
                raise RuntimeError("curl proxy health check failed")
            observed = _public_ipv4(result.stdout.strip(), "observed egress")
            healthy = observed == expected
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            self._record_health(proxy_id, version, {"status": "unhealthy", "checked_at": checked_at,
                                                    "observed_egress_ip": None})
            if isinstance(exc, RuntimeError) and str(exc) == "curl proxy health check failed":
                raise
            raise RuntimeError("proxy health check failed") from exc
        evidence = {"status": "healthy" if healthy else "unhealthy", "checked_at": checked_at,
                    "observed_egress_ip": observed}
        self._record_health(proxy_id, version, evidence)
        if not healthy:
            raise RuntimeError("proxy egress does not match expected pinned IP")
        return {"id": proxy_id, "healthy": True, "expected_egress_ip": expected,
                "observed_egress_ip": observed, "checked_at": checked_at}

    def _record_health(self, proxy_id, expected_version, evidence):
        with self._lock():
            registry = self._load()
            record = registry["proxies"].get(proxy_id)
            if record is None or record["secret_version"] != expected_version:
                raise RuntimeError("proxy changed during health check; result was not recorded")
            record["last_health"] = evidence
            record["updated_at"] = int(time.time())
            self._save(registry)
