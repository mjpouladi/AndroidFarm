#!/usr/bin/env python3
"""Plan, install, and diagnose the Android Farm host integration.

The default command is ``plan``.  ``apply`` is deliberately idempotent: it
creates a content-addressed release, updates only installer-owned files, and
never replaces device data, proxy secrets, inventory, backups, or an existing
APK trust policy.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # Direct execution (`python3 installer/install.py`) otherwise exposes only
    # the installer directory, while sizing and catalog rendering use `ops`.
    sys.path.insert(0, str(PROJECT_ROOT))
SUPPORTED_UBUNTU = {"22.04", "24.04"}
MINIMUM_COMPOSE = (2, 33, 1)
RUNTIME_COMPOSE_PROJECT = "android-farm-runtime"
DEFAULT_HTTP_PORT = 18080
FIRST_ADB_PORT = 5551
LAST_ADB_PORT = 13742
GATEWAY_CONTAINER = "android-farm-gateway"
BINDER_DEVICE_NAMES = ("binder", "hwbinder", "vndbinder")
RELEASE_ENTRIES = (
    ".dockerignore",
    "provisioner.py",
    "generate_farm.py",
    "docker-compose.yml",
    "docker-compose.farm.yml",
    "images",
    "services",
    "monitoring",
    "ansible",
    "ops",
    "traefik",
    "web",
)
IGNORED_RELEASE_NAMES = {"__pycache__", ".pytest_cache", "node_modules", "dist"}
IMAGE_DEFAULTS = {
    "REDROID_IMAGE": "redroid/redroid:12.0.0-latest",
    "PROXY_IMAGE": "android-farm/proxy:1",
    "SCREEN_IMAGE": "android-farm/screen:1",
}
ENV_KEYS = {
    "FARM_DOMAIN",
    "CONSOLE_DOMAIN",
    "COOLIFY_NETWORK",
    "FARM_SECRETS_DIR",
    "FARM_RELEASE_ID",
    "GRAFANA_DOMAIN",
    "GRAFANA_ADMIN_USER",
    "GRAFANA_PASSWORD_FILE",
    "PROMETHEUS_RETENTION",
    "PROMETHEUS_RETENTION_SIZE",
    "FARM_HTTP_BIND",
    "FARM_HTTP_PORT",
    "FARM_HTTP_AUTH_FILE",
    "FARM_HTTP_AUTH_REVISION",
    "FARM_TRAEFIK_ENABLED",
    "GRAFANA_ROOT_URL",
    "GRAFANA_SERVE_FROM_SUB_PATH",
    *IMAGE_DEFAULTS,
}
PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


@dataclass(frozen=True)
class Paths:
    release_root: Path = Path("/opt/android-farm/releases")
    config_dir: Path = Path("/etc/android-farm")
    state_dir: Path = Path("/var/lib/android-farm")
    backup_dir: Path = Path("/var/backups/android-farm")
    data_root: Path = Path("/opt/farm/data/instances")
    wrapper: Path = Path("/usr/local/sbin/device-provisioner")
    traefik_dynamic_dir: Path = Path("/data/coolify/proxy/dynamic")


@dataclass(frozen=True)
class Settings:
    source: Path
    farm_domain: str
    console_domain: str
    coolify_network: str
    compose_project: str
    paths: Paths
    skip_host_bootstrap: bool = False
    catalog_count: int | None = None
    auth_user: str | None = None
    auth_password_file: Path | None = None
    access_mode: str = "domain"
    public_ip: str | None = None
    http_port: int = DEFAULT_HTTP_PORT


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    remediation: str | None = None


@contextmanager
def lifecycle_lock(path: Path = Path('/run/lock/android-farm.lock')):
    """Exclude start/stop/backup while activating host release configuration."""
    if os.name != 'posix':
        raise RuntimeError('lifecycle locking is supported only on the Linux host')
    import fcntl
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def parse_os_release(text: str) -> dict[str, str]:
    """Parse the small KEY=VALUE grammar used by /etc/os-release."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def normalize_domain(value: str) -> str:
    """Return a lower-case ASCII hostname and reject URLs and wildcards."""
    value = value.strip().rstrip(".")
    if not value or any(character in value for character in "/:@?#*[]"):
        raise ValueError("domain must be a hostname without scheme, path, port, or wildcard")
    try:
        ascii_name = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("domain is not valid IDNA") from exc
    if len(ascii_name) > 253 or "." not in ascii_name:
        raise ValueError("domain must be a fully-qualified hostname")
    if any(not DOMAIN_LABEL_RE.fullmatch(label) for label in ascii_name.split(".")):
        raise ValueError("domain contains an invalid DNS label")
    return ascii_name


def normalize_public_ip(value: str) -> str:
    """Return a usable IPv4 listener address; private routed addresses are valid."""
    try:
        address = ipaddress.ip_address(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("--ip must be a valid IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("--ip must be an IPv4 address")
    if (address.is_unspecified or address.is_loopback or address.is_link_local or
            address.is_multicast or address.is_reserved):
        raise ValueError("--ip must be a routable host address (private IPv4 is allowed)")
    return str(address)


def normalize_http_port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise ValueError("--port must be an integer between 1024 and 65535")
    if FIRST_ADB_PORT <= value <= LAST_ADB_PORT:
        raise ValueError(
            f"--port overlaps the reserved Android ADB range {FIRST_ADB_PORT}-{LAST_ADB_PORT}"
        )
    return value


def access_origin(*, access_mode: str, farm_domain: str,
                  public_ip: str | None, http_port: int) -> str:
    """Build the browser origin without ever treating an IP as a DNS suffix."""
    normalize_http_port(http_port)
    if access_mode == "domain":
        return f"https://{normalize_domain(farm_domain)}"
    if access_mode == "ip" and public_ip is not None:
        return f"http://{normalize_public_ip(public_ip)}:{http_port}"
    raise ValueError("access_mode must be domain, or ip with an explicit IPv4 address")


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|v)(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        raise ValueError(f"cannot parse semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def binder_devices_ready(device_root: Path = Path("/dev")) -> bool:
    """Return true only when all Redroid binder character devices exist.

    A binderfs control node merely permits creating devices; it is not itself
    one of the three device nodes consumed by this Redroid configuration.
    """
    return all((device_root / name).exists() for name in BINDER_DEVICE_NAMES)


def validate_project(value: str) -> str:
    if not PROJECT_RE.fullmatch(value or ""):
        raise ValueError("Compose project must be 1-63 safe characters")
    return value


def select_coolify_network(networks: Iterable[str], proxy_networks: Iterable[str] = ()) -> str | None:
    """Select deterministically, returning None when discovery is ambiguous."""
    all_names = {name.strip() for name in networks if name.strip()}
    attached = {name.strip() for name in proxy_networks if name.strip()}
    if "coolify" in all_names:
        return "coolify"
    attached_candidates = sorted(
        name for name in attached if name in all_names and name not in {"bridge", "host", "none"}
    )
    coolify_attached = [name for name in attached_candidates if "coolify" in name.lower()]
    if len(coolify_attached) == 1:
        return coolify_attached[0]
    named = sorted(name for name in all_names if "coolify" in name.lower())
    if len(named) == 1:
        return named[0]
    if len(attached_candidates) == 1:
        return attached_candidates[0]
    return None


def compose_project_from_labels(labels: Mapping[str, object] | None) -> str | None:
    value = (labels or {}).get("com.docker.compose.project")
    if isinstance(value, str) and PROJECT_RE.fullmatch(value):
        return value
    return None


def anchor_release_from_labels(labels: Mapping[str, object] | None) -> str | None:
    value = (labels or {}).get("farm.release")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) else None


def parse_env(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in ENV_KEYS and "\n" not in value and "\r" not in value:
            result[key] = value
    return result


def build_env(existing: Mapping[str, str], *, farm_domain: str, console_domain: str,
              network: str, secret_dir: Path, release_id: str,
              access_mode: str = "domain", public_ip: str | None = None,
              http_port: int = DEFAULT_HTTP_PORT,
              auth_file: Path | None = None,
              auth_revision: str | None = None) -> dict[str, str]:
    origin = access_origin(access_mode=access_mode, farm_domain=farm_domain,
                           public_ip=public_ip, http_port=http_port)
    if access_mode == "ip":
        # access_origin validated and canonicalized this value above.
        host = origin.removeprefix("http://").rsplit(":", 1)[0]
        farm_domain = host
        console_domain = host
    else:
        farm_domain = normalize_domain(farm_domain)
        console_domain = normalize_domain(console_domain)
    previous_farm = existing.get("FARM_DOMAIN")
    previous_grafana = existing.get("GRAFANA_DOMAIN")
    previous_grafana_was_derived = bool(
        previous_farm and previous_grafana in {previous_farm, f"metrics.{previous_farm}"}
    )
    if auth_revision is None:
        auth_revision = existing.get("FARM_HTTP_AUTH_REVISION",
                                     hashlib.sha256(b"").hexdigest())
    if not re.fullmatch(r"[0-9a-f]{64}", auth_revision):
        raise ValueError("HTTP auth revision must be a lowercase SHA-256 digest")
    result = {key: value for key, value in existing.items() if key in ENV_KEYS}
    for key, default in IMAGE_DEFAULTS.items():
        result.setdefault(key, default)
    result.update({
        "FARM_DOMAIN": farm_domain,
        "CONSOLE_DOMAIN": console_domain,
        "COOLIFY_NETWORK": network,
        "FARM_SECRETS_DIR": str(secret_dir),
        "FARM_RELEASE_ID": release_id,
        "FARM_HTTP_BIND": "0.0.0.0" if access_mode == "ip" else "127.0.0.1",
        "FARM_HTTP_PORT": str(http_port),
        "FARM_HTTP_AUTH_FILE": str(auth_file or
                                   (Paths.traefik_dynamic_dir / "farm-users.htpasswd")),
        "FARM_HTTP_AUTH_REVISION": auth_revision,
        "FARM_TRAEFIK_ENABLED": "false" if access_mode == "ip" else "true",
    })
    if access_mode == "ip":
        result["GRAFANA_DOMAIN"] = farm_domain
    elif not previous_grafana or previous_grafana_was_derived:
        result["GRAFANA_DOMAIN"] = f"metrics.{farm_domain}"
    result["GRAFANA_ROOT_URL"] = (f"{origin}/metrics/" if access_mode == "ip" else
                                  f"https://{result['GRAFANA_DOMAIN']}/")
    result["GRAFANA_SERVE_FROM_SUB_PATH"] = "true" if access_mode == "ip" else "false"
    result.setdefault("GRAFANA_ADMIN_USER", "admin")
    result.setdefault("GRAFANA_PASSWORD_FILE", str(secret_dir.parent / "monitoring" /
                                                    "grafana-admin-password"))
    result.setdefault("PROMETHEUS_RETENTION", "30d")
    result.setdefault("PROMETHEUS_RETENTION_SIZE", "20GB")
    return result


def render_env(values: Mapping[str, str]) -> str:
    lines = []
    for key in sorted(values):
        value = values[key]
        if key not in ENV_KEYS or not value or any(character in value for character in "\r\n\0"):
            raise ValueError(f"invalid environment value for {key}")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def build_provisioner_config(existing: Mapping[str, object], *, release_dir: Path,
                             project: str, farm_domain: str, paths: Paths,
                             access_mode: str = "domain", public_ip: str | None = None,
                             http_port: int = DEFAULT_HTTP_PORT) -> dict[str, object]:
    validate_project(project)
    origin = access_origin(access_mode=access_mode, farm_domain=farm_domain,
                           public_ip=public_ip, http_port=http_port)
    value = dict(existing)
    value.update({
        "compose_file": str(release_dir / "docker-compose.farm.yml"),
        "compose_project": project,
        "compose_env_file": str(paths.config_dir / "compose.env"),
        "console_url": origin,
        "access_mode": access_mode,
        "secret_dir": str(paths.config_dir / "secrets"),
        "backup_dir": str(paths.backup_dir),
        "state_dir": str(paths.state_dir),
        "apk_trust_file": str(paths.config_dir / "apk-trust.json"),
        "proxy_registry": str(paths.state_dir / "proxies.json"),
        "proxy_store_dir": str(paths.config_dir / "proxies"),
    })
    return value


def sizing_summary(resource_report: Mapping[str, object] | None, catalog_count: int,
                   active_count: int) -> dict[str, object]:
    report = dict(resource_report or {})
    capacity = int(report.get("active_capacity", report.get("capacity", 0)))
    catalog_additions = int(report.get("catalog_capacity", 0))
    return {
        "catalog_devices": max(0, int(catalog_count)),
        "active_devices": max(0, int(active_count)),
        "calculated_active_limit": max(0, capacity),
        "available_active_slots": max(0, capacity - max(0, int(active_count))),
        "safe_additional_catalog_devices": max(0, catalog_additions),
        "resource_report": report,
    }


def _installed_catalog_count(state_dir: Path) -> int:
    try:
        state = _read_json_if_regular(state_dir / "install-state.json")
        value = state.get("catalog_count", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
    except (OSError, RuntimeError):
        return 0


def _allocated_device_count(state_dir: Path) -> int:
    try:
        path = state_dir / "inventory.json"
        if path.is_symlink() or not path.is_file():
            return 0
        inventory = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(inventory, list):
            return len(inventory)
        devices = inventory.get("devices", {}) if isinstance(inventory, dict) else {}
        if isinstance(devices, dict):
            return len(devices)
        if isinstance(devices, list):
            return len(devices)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return 0


def choose_catalog_count(settings: Settings, sizing: Mapping[str, object]) -> int:
    """Choose an auto catalog without ever shrinking installed/allocated IDs."""
    from ops.device_ids import DEVICE_LIMIT
    previous = _installed_catalog_count(settings.paths.state_dir)
    allocated = _allocated_device_count(settings.paths.state_dir)
    floor = max(1, previous, allocated)
    if settings.catalog_count is not None:
        if settings.catalog_count < floor:
            raise RuntimeError(
                f"catalog count cannot shrink below installed/allocated floor {floor}"
            )
        return settings.catalog_count
    additional = sizing.get("safe_additional_catalog_devices", 0)
    additional = additional if isinstance(additional, int) and not isinstance(additional, bool) else 0
    return min(DEVICE_LIMIT, max(floor, allocated + max(0, additional)))


def _release_files(source: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for entry in RELEASE_ENTRIES:
        path = source / entry
        if not path.exists():
            raise RuntimeError(f"release source is missing required entry: {entry}")
        if path.is_symlink():
            raise RuntimeError(f"release source may not contain symlink entry: {entry}")
        if path.is_file():
            files.append((entry, path))
            continue
        for candidate in sorted(path.rglob("*")):
            relative = candidate.relative_to(source)
            if any(part in IGNORED_RELEASE_NAMES for part in relative.parts):
                continue
            if candidate.is_symlink():
                raise RuntimeError(f"release source may not contain symlinks: {relative}")
            if candidate.is_file() and candidate.suffix not in {".pyc", ".pyo"}:
                files.append((relative.as_posix(), candidate))
    return sorted(files)


def _release_content(relative: str, path: Path, catalog_count: int | None = None) -> bytes:
    if relative == "docker-compose.farm.yml" and catalog_count is not None:
        # Render from the selected source checkout in a fresh interpreter. This
        # avoids accidentally importing generate_farm/ops from the installer's
        # own checkout when --source points at a reviewed upgrade candidate.
        program = (
            "import json,sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "from generate_farm import generate; "
            "print(json.dumps(generate(int(sys.argv[2])), indent=2))"
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(path.parent), str(catalog_count)],
            text=True, capture_output=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError("selected source could not render the requested device catalog")
        return (result.stdout.rstrip("\n") + "\n").encode("utf-8")
    return path.read_bytes()


def release_digest(source: Path, catalog_count: int | None = None) -> tuple[str, list[dict[str, object]]]:
    source = source.resolve()
    digest = hashlib.sha256()
    manifest: list[dict[str, object]] = []
    for relative, path in _release_files(source):
        content = _release_content(relative, path, catalog_count)
        file_digest = hashlib.sha256(content).hexdigest()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(str(len(content)).encode("ascii") + b"\0")
        digest.update(content)
        manifest.append({"path": relative, "size": len(content), "sha256": file_digest})
    return digest.hexdigest(), manifest


def verify_release_directory(path: Path, expected_digest: str | None = None) -> tuple[bool, str]:
    """Verify every installed release file against its content manifest."""
    try:
        if path.is_symlink() or not path.is_dir():
            return False, "release is not a real directory"
        if os.name == "posix":
            root_stat = path.stat()
            if root_stat.st_uid != 0 or root_stat.st_mode & 0o022:
                return False, "release directory is not root-owned and protected from group/other writes"
        manifest = _read_json_if_regular(path / ".install-manifest.json")
        records = manifest.get("files")
        if not isinstance(records, list):
            return False, "release manifest has no file list"
        digest = hashlib.sha256()
        expected_files = {".install-manifest.json"}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                return False, "release manifest contains an invalid record"
            relative = Path(record["path"])
            if relative.is_absolute() or ".." in relative.parts:
                return False, "release manifest contains an unsafe path"
            candidate = path / relative
            if candidate.is_symlink() or not candidate.is_file():
                return False, f"release file missing or unsafe: {relative}"
            if os.name == "posix":
                item_stat = candidate.stat()
                if item_stat.st_uid != 0 or item_stat.st_mode & 0o022:
                    return False, f"release file permissions are unsafe: {relative}"
            content = candidate.read_bytes()
            file_digest = hashlib.sha256(content).hexdigest()
            if record.get("size") != len(content) or record.get("sha256") != file_digest:
                return False, f"release file differs from manifest: {relative}"
            normalized = relative.as_posix()
            expected_files.add(normalized)
            digest.update(normalized.encode("utf-8") + b"\0")
            digest.update(str(len(content)).encode("ascii") + b"\0")
            digest.update(content)
        actual_files = set()
        for candidate in path.rglob("*"):
            if candidate.is_symlink():
                return False, f"release contains a symlink: {candidate.relative_to(path)}"
            if candidate.is_file():
                actual_files.add(candidate.relative_to(path).as_posix())
        if actual_files != expected_files:
            return False, "release contains files outside its manifest"
        actual_digest = digest.hexdigest()
        if manifest.get("content_sha256") != actual_digest:
            return False, "release aggregate digest differs from manifest"
        if expected_digest is not None and actual_digest != expected_digest:
            return False, "release digest differs from requested source"
        if manifest.get("release_id") != actual_digest[:16] or path.name != actual_digest[:16]:
            return False, "release directory name does not match its digest"
        return True, actual_digest
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, str(exc)


def _safe_existing_directory(path: Path, mode: int) -> None:
    if not path.is_absolute():
        raise RuntimeError(f"managed directory must be absolute: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise RuntimeError(f"managed path may not traverse a symlink: {current}")
            if current == path and not current.is_dir():
                raise RuntimeError(f"expected a real directory: {path}")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"expected a real directory, not a symlink: {path}")
    else:
        path.mkdir(parents=True, mode=mode)
    path.chmod(mode)
    if os.name == "posix" and os.geteuid() == 0:
        os.chown(path, 0, 0)


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {path}")
    if path.parent.exists() or path.parent.is_symlink():
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise RuntimeError(f"expected a real parent directory: {path.parent}")
    else:
        _safe_existing_directory(path.parent, 0o755)
    if os.name == "posix" and os.geteuid() == 0:
        parent_stat = path.parent.stat()
        if parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022:
            raise RuntimeError(f"managed file parent must be root-owned and protected: {path.parent}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            temporary.chmod(mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        if os.name == "posix" and os.geteuid() == 0:
            os.chown(path, 0, 0)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()


def _read_json_if_regular(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"expected a regular JSON file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON object required: {path}")
    return value


AUTH_USER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def _path_traverses_symlink(path: Path) -> bool:
    """Inspect every existing path component without resolving it first."""
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _private_password(path: Path) -> str:
    """Read one root-private password line without ever placing it in argv."""
    path = Path(path).absolute()
    if _path_traverses_symlink(path) or not path.is_file():
        raise RuntimeError("Basic Auth password file must be a regular file")
    path = path.resolve(strict=True)
    info = path.stat()
    if info.st_size > 4096:
        raise RuntimeError("Basic Auth password file is unexpectedly large")
    if os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o077):
        raise RuntimeError("Basic Auth password file must be root-owned and chmod 0600")
    value = path.read_text(encoding="utf-8")
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RuntimeError("Basic Auth password file must contain one non-empty line")
    return value


def configure_traefik_auth(settings: Settings) -> dict[str, object]:
    """Install the fail-closed middleware and optionally create its bcrypt user."""
    dynamic = settings.paths.traefik_dynamic_dir.absolute()
    if _path_traverses_symlink(dynamic) or not dynamic.is_dir():
        raise RuntimeError(f"Coolify Traefik dynamic directory is unavailable: {dynamic}")
    dynamic = dynamic.resolve(strict=True)
    middleware_source = settings.source / "traefik" / "farm-auth.yml"
    if middleware_source.is_symlink() or not middleware_source.is_file():
        raise RuntimeError("reviewed Traefik middleware source is missing")
    _atomic_write(dynamic / "farm-auth.yml", middleware_source.read_bytes(), 0o644)
    users = dynamic / "farm-users.htpasswd"
    if settings.auth_password_file is not None:
        if not settings.auth_user or not AUTH_USER_RE.fullmatch(settings.auth_user):
            raise RuntimeError("--auth-user must contain 1-64 safe characters")
        password = _private_password(settings.auth_password_file)
        existing_record = ""
        if users.exists() or users.is_symlink():
            valid, detail = private_path_status(users)
            if not valid:
                raise RuntimeError(f"existing Basic Auth user file is unsafe: {detail}")
            existing_record = users.read_text(encoding="utf-8").strip()
        same_user = (existing_record.startswith(settings.auth_user + ":$2") and
                     "\n" not in existing_record)
        verified = False
        if same_user:
            verify = _command(
                ["htpasswd", "-vi", str(users), settings.auth_user],
                input_text=password + "\n",
            )
            verified = verify.returncode == 0
        if not verified:
            result = _command(["htpasswd", "-niB", settings.auth_user],
                              input_text=password + "\n")
            if result.returncode or not result.stdout.startswith(settings.auth_user + ":$2"):
                raise RuntimeError("failed to generate the browser Basic Auth bcrypt record")
            _atomic_write(users, result.stdout.rstrip("\n").encode("utf-8") + b"\n", 0o600)
    if users.is_symlink() or not users.is_file() or not users.read_text(encoding="utf-8").strip():
        raise RuntimeError("Traefik Basic Auth user file is missing; pass --auth-user and --auth-password-file")
    if os.name == "posix":
        info = users.stat()
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise RuntimeError("Traefik Basic Auth user file must be root-owned and chmod 0600")
    return {"middleware": str(dynamic / "farm-auth.yml"), "users_file": str(users),
            "revision": auth_file_revision(users)}


def private_path_status(path: Path, *, directory: bool = False) -> tuple[bool, str]:
    """Describe whether a managed private path has fail-closed ownership/mode."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False, "missing"
    wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if path.is_symlink() or not wanted:
        return False, "not a regular managed " + ("directory" if directory else "file")
    if os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o077):
        return False, "must be root-owned with no group/other permissions"
    return True, "private permissions verified"


def auth_file_revision(path: Path) -> str:
    """Return a non-secret deployment trigger for a protected htpasswd file."""
    valid, detail = private_path_status(path)
    if not valid:
        raise RuntimeError(f"Basic Auth user file is unsafe: {detail}")
    content = path.read_bytes()
    if not content.strip():
        raise RuntimeError("Basic Auth user file is empty")
    return hashlib.sha256(content).hexdigest()


def install_release(source: Path, release_root: Path,
                    catalog_count: int | None = None) -> tuple[Path, str, bool]:
    """Copy the operational allowlist into a concrete content-addressed tree."""
    source = source.resolve()
    full_digest, files = release_digest(source, catalog_count)
    release_id = full_digest[:16]
    destination = release_root / release_id
    _safe_existing_directory(release_root, 0o755)
    if destination.exists():
        valid, detail = verify_release_directory(destination, full_digest)
        if not valid:
            raise RuntimeError(f"existing release failed verification ({detail}): {destination}")
        return destination, release_id, False
    staging = release_root / f".{release_id}.tmp-{os.getpid()}"
    if staging.exists() or staging.is_symlink():
        raise RuntimeError(f"stale release staging path exists: {staging}")
    staging.mkdir(mode=0o700)
    try:
        for record in files:
            relative = Path(str(record["path"]))
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_release_content(str(record["path"]), source / relative, catalog_count))
        manifest = {"schema_version": 1, "release_id": release_id,
                    "content_sha256": full_digest, "files": files}
        (staging / ".install-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for candidate in sorted(staging.rglob("*"), reverse=True):
            if candidate.is_symlink():
                raise RuntimeError(f"unexpected symlink in staged release: {candidate}")
            if os.name == "posix" and os.geteuid() == 0:
                os.chown(candidate, 0, 0)
            candidate.chmod(0o555 if candidate.is_dir() else 0o444)
        if os.name == "posix" and os.geteuid() == 0:
            os.chown(staging, 0, 0)
        staging.chmod(0o555)
        staging.rename(destination)
    finally:
        if staging.exists():
            # Staging is installer-owned and contains no persistent state.
            shutil.rmtree(staging)
    return destination, release_id, True


def _command(args: Sequence[str], *, timeout: int = 30, check: bool = False,
             input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), text=True, capture_output=True, timeout=timeout,
                          check=check, input=input_text)


def _json_command(args: Sequence[str]) -> object | None:
    try:
        result = _command(args)
        return json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _managed_gateway_http_bindings(port: int) -> set[str]:
    """Return host IPs owned by the attested running farm gateway."""
    payload = _json_command(["docker", "inspect", GATEWAY_CONTAINER])
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return set()
    item = payload[0]
    state = item.get("State") if isinstance(item.get("State"), dict) else {}
    config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    network = (item.get("NetworkSettings")
               if isinstance(item.get("NetworkSettings"), dict) else {})
    ports = network.get("Ports") if isinstance(network.get("Ports"), dict) else {}
    bindings = ports.get("8080/tcp") if isinstance(ports.get("8080/tcp"), list) else []
    if not (state.get("Running") and labels.get("farm.stack") == "core" and
            labels.get("farm.role") == "gateway"):
        return set()
    return {
        str(binding.get("HostIp") or "0.0.0.0")
        for binding in bindings
        if isinstance(binding, dict) and binding.get("HostPort") == str(port)
    }


def managed_gateway_owns_http_port(port: int) -> bool:
    """Recognize only the running, labelled farm gateway as an idempotent owner."""
    return bool(_managed_gateway_http_bindings(port))


def _published_port_conflicts(port: int, bind_host: str) -> bool | None:
    """Inspect Docker's allocator; None means the inventory could not be trusted."""
    try:
        listed = _command(["docker", "ps", "--filter", f"publish={port}", "-q"])
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode:
        return None
    identifiers = listed.stdout.split()
    if not identifiers:
        return False
    payload = _json_command(["docker", "inspect", *identifiers])
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        return None
    for item in payload:
        config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        name = str(item.get("Name", "")).lstrip("/")
        own_gateway = (name == GATEWAY_CONTAINER and labels.get("farm.stack") == "core" and
                       labels.get("farm.role") == "gateway")
        if own_gateway:
            continue
        network = item.get("NetworkSettings") if isinstance(item.get("NetworkSettings"), dict) else {}
        ports = network.get("Ports") if isinstance(network.get("Ports"), dict) else {}
        for bindings in ports.values():
            if not isinstance(bindings, list):
                continue
            for binding in bindings:
                if not isinstance(binding, dict) or binding.get("HostPort") != str(port):
                    continue
                host = str(binding.get("HostIp") or "0.0.0.0")
                if bind_host == "0.0.0.0" or host in {"0.0.0.0", "127.0.0.1", "::"}:
                    return True
    return False


def _loopback_is_only_host_listener(port: int) -> bool:
    """Distinguish our loopback gateway from another-NIC listeners during widening."""
    try:
        result = _command(["ss", "-H", "-ltn", "sport", "=", f":{port}"])
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode:
        return False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            return False
        endpoint = fields[3]
        if endpoint not in {f"127.0.0.1:{port}", f"127.0.0.1%lo:{port}"}:
            return False
    return True


def http_port_available(port: int, bind_host: str) -> bool:
    """Check the requested binding and permit only our gateway idempotently."""
    port = normalize_http_port(port)
    if bind_host not in {"0.0.0.0", "127.0.0.1"}:
        raise ValueError("HTTP bind host must be loopback or all IPv4 interfaces")
    gateway_bindings = _managed_gateway_http_bindings(port)
    docker_conflict = _published_port_conflicts(port, bind_host)
    if docker_conflict is True:
        return False
    if gateway_bindings:
        # Narrowing an all-interface gateway to loopback is safe. Widening a
        # loopback gateway requires a complete Docker inventory and no host
        # listener other than that existing loopback publication.
        if bind_host == "127.0.0.1" or "0.0.0.0" in gateway_bindings:
            return docker_conflict is not None
        return docker_conflict is False and _loopback_is_only_host_listener(port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((bind_host, port))
        return docker_conflict is False
    except OSError:
        return False
    finally:
        probe.close()


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return _command(["docker", "info", "--format", "{{.ServerVersion}}"],
                        timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def compose_version() -> str | None:
    if not shutil.which("docker"):
        return None
    try:
        result = _command(["docker", "compose", "version", "--short"])
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def docker_endpoint() -> str | None:
    if not shutil.which("docker"):
        return None
    try:
        result = _command(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
        if result.returncode:
            return None
        return os.environ.get("DOCKER_HOST") or result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def docker_user_chain_ready() -> bool:
    if not shutil.which("iptables"):
        return False
    try:
        return _command(["iptables", "-w", "-S", "DOCKER-USER"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def docker_network_names() -> list[str]:
    if not docker_available():
        return []
    result = _command(["docker", "network", "ls", "--format", "{{.Name}}"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []


def docker_network_inventory() -> list[dict[str, object]] | None:
    """Return all local Docker network inspections, or None on probe failure."""
    if not docker_available():
        return None
    try:
        listed = _command(["docker", "network", "ls", "-q"])
        if listed.returncode:
            return None
        identifiers = listed.stdout.split()
        inventory: list[dict[str, object]] = []
        for offset in range(0, len(identifiers), 100):
            result = _command(["docker", "network", "inspect", *identifiers[offset:offset + 100]])
            if result.returncode:
                return None
            payload = json.loads(result.stdout)
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                return None
            inventory.extend(payload)
        return inventory
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def docker_network_pool_conflicts(networks: Iterable[Mapping[str, object]]) -> list[dict[str, str]]:
    """Find non-farm Docker subnets overlapping either reserved Android Farm pool.

    Existing runtime networks are accepted only when their name, subnet, driver
    and Compose project all attest to the exact slot generated by this release.
    This keeps repeated installer runs idempotent without trusting a lookalike
    network created outside the host-owned runtime project.
    """
    from ops.device_ids import CONTROL_POOL, EGRESS_POOL, device_index, network_plan

    pools = (("egress", EGRESS_POOL), ("control", CONTROL_POOL))
    conflicts: list[dict[str, str]] = []
    for item in networks:
        name = str(item.get("Name", ""))
        driver = str(item.get("Driver", ""))
        labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
        compose_project = str(labels.get("com.docker.compose.project", ""))
        options = item.get("Options") if isinstance(item.get("Options"), dict) else {}
        internal = bool(item.get("Internal"))
        ipam = item.get("IPAM") if isinstance(item.get("IPAM"), dict) else {}
        configs = ipam.get("Config") if isinstance(ipam.get("Config"), list) else []
        for config in configs:
            if not isinstance(config, dict) or not isinstance(config.get("Subnet"), str):
                continue
            try:
                subnet = ipaddress.ip_network(config["Subnet"], strict=False)
            except ValueError:
                conflicts.append({"network": name or "<unnamed>", "subnet": config["Subnet"],
                                  "reserved_pool": "invalid-subnet"})
                continue
            if subnet.version != 4:
                continue
            for kind, pool in pools:
                if not subnet.overlaps(pool):
                    continue
                match = re.fullmatch(rf"farm-{kind}-(num[0-9]{{2,6}})", name)
                managed = False
                if match and driver == "bridge" and compose_project == RUNTIME_COMPOSE_PROJECT:
                    try:
                        plan = network_plan(device_index(match.group(1), aliases=False))
                        topology_ok = (internal if kind == "control" else
                                       not internal and
                                       options.get("com.docker.network.bridge.name") == plan["bridge"])
                        managed = str(subnet) == plan[f"{kind}_subnet"] and topology_ok
                    except ValueError:
                        managed = False
                if not managed:
                    conflicts.append({"network": name or "<unnamed>", "subnet": str(subnet),
                                      "reserved_pool": str(pool)})
                break
    return conflicts


def proxy_attached_networks() -> list[str]:
    if not docker_available():
        return []
    ids = _command(["docker", "ps", "-q"]).stdout.split()
    payload = _json_command(["docker", "inspect", *ids]) if ids else []
    found: set[str] = set()
    for item in payload if isinstance(payload, list) else []:
        name = str(item.get("Name", "")).lower()
        image = str(item.get("Config", {}).get("Image", "")).lower()
        labels = item.get("Config", {}).get("Labels") or {}
        haystack = " ".join((name, image, json.dumps(labels, sort_keys=True))).lower()
        if "traefik" not in haystack and "coolify-proxy" not in haystack:
            continue
        found.update((item.get("NetworkSettings", {}).get("Networks") or {}).keys())
    return sorted(found)


def anchor_labels() -> dict[str, object]:
    payload = _json_command(["docker", "inspect", "farm-anchor"])
    if not isinstance(payload, list) or not payload:
        return {}
    labels = payload[0].get("Config", {}).get("Labels") or {}
    return labels if isinstance(labels, dict) else {}


def active_device_count() -> int:
    if not docker_available():
        return 0
    result = _command(["docker", "ps", "--filter", "label=farm.role=android", "--format", "{{.Names}}"])
    return len([line for line in result.stdout.splitlines() if line.strip()]) if result.returncode == 0 else 0


def catalog_device_count(compose_file: Path) -> int:
    try:
        document = json.loads(compose_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    services = document.get("services", {}) if isinstance(document, dict) else {}
    return len([name for name in services if re.fullmatch(r"android-num\d{2,}", name)])


def resource_probe(data_root: Path = Path("/opt/farm/data/instances")) -> dict[str, object] | None:
    try:
        from ops import resources
        return resources.probe(data_root=data_root)
    except (ImportError, KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return None


def _host_release() -> dict[str, str]:
    try:
        return parse_os_release(Path("/etc/os-release").read_text(encoding="utf-8"))
    except OSError:
        return {}


def discover(settings: Settings) -> dict[str, object]:
    # Validate the access contract before any plan/apply action can proceed.
    access_origin(access_mode=settings.access_mode, farm_domain=settings.farm_domain,
                  public_ip=settings.public_ip, http_port=settings.http_port)
    http_bind = "0.0.0.0" if settings.access_mode == "ip" else "127.0.0.1"
    networks = docker_network_names()
    network_inventory = docker_network_inventory()
    attached = proxy_attached_networks()
    detected_network = select_coolify_network(networks, attached)
    labels = anchor_labels() if docker_available() else {}
    detected_project = compose_project_from_labels(labels)
    chosen_network = detected_network if settings.coolify_network == "auto" else settings.coolify_network
    chosen_project = detected_project if settings.compose_project == "auto" else settings.compose_project
    report = resource_probe(settings.paths.data_root) if docker_available() and platform.system() == "Linux" else None
    return {
        "os_release": _host_release(),
        "architecture": platform.machine(),
        "inside_container": Path("/.dockerenv").exists(),
        "docker_ready": docker_available(),
        "docker_endpoint": docker_endpoint(),
        "compose_version": compose_version(),
        "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
        "docker_user_chain": docker_user_chain_ready(),
        "networks": networks,
        "network_inventory_ready": network_inventory is not None,
        "network_pool_conflicts": docker_network_pool_conflicts(network_inventory or []),
        "proxy_networks": attached,
        "coolify_network": chosen_network,
        "detected_coolify_network": detected_network,
        "compose_project": chosen_project,
        "detected_compose_project": detected_project,
        "anchor_present": bool(detected_project),
        "project_mismatch": bool(settings.compose_project != "auto" and detected_project and
                                 detected_project != settings.compose_project),
        "anchor_release": anchor_release_from_labels(labels),
        "http_bind": http_bind,
        "http_port_available": http_port_available(settings.http_port, http_bind),
        "sizing": sizing_summary(report,
                                  catalog_device_count(settings.source / "docker-compose.farm.yml"),
                                  active_device_count()),
    }


def _bootstrap_required() -> bool:
    commands = ("curl", "git", "rsync", "docker", "iptables", "iptables-restore",
                "htpasswd", "apksigner", "aapt", "restic")
    if any(not shutil.which(command) for command in commands):
        return True
    try:
        result = _command(["docker", "compose", "version", "--short"])
        return result.returncode != 0 or parse_version(result.stdout) < MINIMUM_COMPOSE
    except (OSError, ValueError, subprocess.SubprocessError):
        return True


def _compose_ready() -> bool:
    version = compose_version()
    if version is None:
        return False
    try:
        return parse_version(version) >= MINIMUM_COMPOSE
    except ValueError:
        return False


def prepare_host(source: Path, skip: bool = False) -> None:
    del source
    if not skip:
        if _bootstrap_required():
            os.environ["DEBIAN_FRONTEND"] = "noninteractive"
            subprocess.run(["apt-get", "update"], check=True)
            subprocess.run(["apt-get", "install", "-y", "ca-certificates", "curl", "git", "python3",
                            "iptables", "apache2-utils", "apksigner", "aapt", "rsync", "restic"], check=True)
            if not shutil.which("docker"):
                conflicts = []
                for package in ("docker.io", "docker-compose", "containerd", "runc", "podman-docker"):
                    result = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package],
                                            capture_output=True, text=True)
                    if result.returncode == 0 and result.stdout == "install ok installed":
                        conflicts.append(package)
                if conflicts:
                    raise RuntimeError("existing container packages require operator migration: " +
                                       ", ".join(conflicts))
                subprocess.run(["install", "-m", "0755", "-d", "/etc/apt/keyrings"], check=True)
                key = subprocess.run(["curl", "-fsSL", "https://download.docker.com/linux/ubuntu/gpg"],
                                     check=True, capture_output=True).stdout
                _atomic_write(Path("/etc/apt/keyrings/docker.asc"), key, 0o644)
                release = _host_release()
                architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
                source_text = ("Types: deb\nURIs: https://download.docker.com/linux/ubuntu\n"
                               f"Suites: {release['VERSION_CODENAME']}\nComponents: stable\n"
                               f"Architectures: {architecture}\nSigned-By: /etc/apt/keyrings/docker.asc\n")
                _atomic_write(Path("/etc/apt/sources.list.d/android-farm-docker.sources"),
                              source_text.encode(), 0o644)
                subprocess.run(["apt-get", "update"], check=True)
                subprocess.run(["apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
                                "docker-buildx-plugin", "docker-compose-plugin"], check=True)
            if not _compose_ready():
                # Existing distro Docker installations may lack the v2 plugin.
                # Try the native Ubuntu package first, then the Docker-repo name.
                for package in ("docker-compose-v2", "docker-compose-plugin"):
                    subprocess.run(["apt-get", "install", "-y", package], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if _compose_ready():
                        break
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "enable", "--now", "docker"], check=True)
        compose = subprocess.check_output(["docker", "compose", "version", "--short"], text=True)
        if parse_version(compose) < MINIMUM_COMPOSE:
            raise RuntimeError("Docker Compose >= 2.33.1 is required")
        subprocess.run(["modprobe", "binder_linux", "devices=binder,hwbinder,vndbinder"], check=True)
        _atomic_write(Path("/etc/modules-load.d/android-farm.conf"), b"binder_linux\n", 0o644)
        _atomic_write(Path("/etc/modprobe.d/android-farm.conf"),
                      b"options binder_linux devices=binder,hwbinder,vndbinder\n", 0o644)
    if not binder_devices_ready():
        missing = ", ".join(name for name in BINDER_DEVICE_NAMES
                            if not (Path("/dev") / name).exists())
        raise RuntimeError(
            "Redroid binder devices unavailable (missing: " + missing +
            "); load binder_linux with devices=binder,hwbinder,vndbinder"
        )


def _existing_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"expected regular environment file: {path}")
    if os.name == "posix":
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise RuntimeError(f"existing environment file must be root-owned and chmod 0600: {path}")
    return parse_env(path.read_text(encoding="utf-8"))


def install_managed_files(settings: Settings, discovered: Mapping[str, object]) -> dict[str, object]:
    """Install release/configuration. Caller enforces host/root requirements."""
    paths = settings.paths
    for path, mode in ((paths.config_dir, 0o700), (paths.config_dir / "secrets", 0o700),
                       (paths.config_dir / "proxies", 0o700),
                       (paths.config_dir / "monitoring", 0o700),
                       (paths.state_dir, 0o700), (paths.backup_dir, 0o700),
                       (paths.data_root, 0o700)):
        _safe_existing_directory(path, mode)
    grafana_password = paths.config_dir / "monitoring" / "grafana-admin-password"
    if not grafana_password.exists():
        _atomic_write(grafana_password, (secrets.token_urlsafe(36) + "\n").encode(), 0o600)
    elif grafana_password.is_symlink() or not grafana_password.is_file():
        raise RuntimeError(f"Grafana password must be a regular file: {grafana_password}")
    elif os.name == "posix" and (grafana_password.stat().st_uid != 0 or
                                  grafana_password.stat().st_mode & 0o077):
        raise RuntimeError(f"Grafana password must be root-owned and chmod 0600: {grafana_password}")
    sizing = discovered.get("sizing") if isinstance(discovered.get("sizing"), dict) else {}
    catalog_count = choose_catalog_count(settings, sizing)
    release_dir, release_id, created = install_release(settings.source, paths.release_root, catalog_count)
    network = discovered.get("coolify_network")
    if not isinstance(network, str):
        raise RuntimeError("Coolify network was not detected; pass --coolify-network explicitly")
    existing_env = _existing_env(paths.config_dir / "compose.env")
    auth_file = paths.traefik_dynamic_dir / "farm-users.htpasswd"
    auth_revision = auth_file_revision(auth_file)
    env = build_env(existing_env, farm_domain=settings.farm_domain,
                    console_domain=settings.console_domain, network=network,
                    secret_dir=paths.config_dir / "secrets", release_id=release_id,
                    access_mode=settings.access_mode, public_ip=settings.public_ip,
                    http_port=settings.http_port,
                    auth_file=auth_file, auth_revision=auth_revision)
    rendered_env = render_env(env).encode()
    # This file is the handoff to Coolify. Keep it separate from the live CLI
    # environment so staging an upgrade cannot alter a still-active release.
    _atomic_write(paths.config_dir / "coolify.env", rendered_env, 0o600)
    trust_file = paths.config_dir / "apk-trust.json"
    if not trust_file.exists():
        _atomic_write(trust_file, b'{"packages": {}}\n', 0o600)
    elif trust_file.is_symlink() or not trust_file.is_file():
        raise RuntimeError(f"APK trust policy must be a regular file: {trust_file}")
    else:
        if os.name == "posix" and trust_file.stat().st_uid != 0:
            raise RuntimeError(f"APK trust policy must be root-owned: {trust_file}")
        trust_file.chmod(0o600)
    project = discovered.get("compose_project")
    anchor_release = discovered.get("anchor_release")
    config_path = paths.config_dir / "provisioner.json"
    existing_config = _read_json_if_regular(config_path)
    live_compose = existing_config.get("compose_file")
    live_release = Path(live_compose).parent if isinstance(live_compose, str) else None
    active_count = int(sizing.get("active_devices", 0))
    configured = False
    waiting_reason: str | None = None
    if not discovered.get("anchor_present"):
        waiting_reason = "farm-anchor is not deployed; deploy the core Compose in Coolify and rerun apply"
    elif discovered.get("project_mismatch"):
        waiting_reason = (f"farm-anchor belongs to {discovered.get('detected_compose_project')}, not the "
                          f"requested project {project}")
    elif not isinstance(project, str):
        waiting_reason = "farm-anchor has no valid Compose project label"
    elif isinstance(anchor_release, str) and anchor_release != release_id:
        waiting_reason = (f"Coolify anchor release {anchor_release} differs from staged release {release_id}; "
                          "deploy the same release and rerun apply")
    elif live_release is not None and live_release != release_dir and active_count > 0:
        waiting_reason = (f"{active_count} Android device(s) are active; stop them before activating "
                          f"release {release_id}")
    else:
        validate_project(project)
        _atomic_write(paths.config_dir / "compose.env", rendered_env, 0o600)
        # Coolify owns only the core stack.  On-demand devices use a stable,
        # separate project so a Coolify redeploy cannot remove them as orphans.
        config = build_provisioner_config(existing_config, release_dir=release_dir,
                                          project=RUNTIME_COMPOSE_PROJECT,
                                          farm_domain=settings.farm_domain, paths=paths,
                                          access_mode=settings.access_mode,
                                          public_ip=settings.public_ip,
                                          http_port=settings.http_port)
        _atomic_write(config_path, (json.dumps(config, indent=2, sort_keys=True) + "\n").encode(), 0o600)
        wrapper = ("#!/bin/sh\nset -eu\nexec /usr/bin/python3 " +
                   shlex.quote(str(release_dir / "provisioner.py")) + ' "$@"\n')
        _atomic_write(paths.wrapper, wrapper.encode(), 0o755)
        configured = True
    if not configured and not (paths.config_dir / "compose.env").exists():
        # On first installation there is no old release to protect. Providing
        # both names keeps the initial Coolify handoff straightforward.
        _atomic_write(paths.config_dir / "compose.env", rendered_env, 0o600)
    state = {
        "schema_version": 1,
        "updated_at": int(time.time()),
        "release_id": release_id,
        "release_dir": str(release_dir),
        "farm_domain": settings.farm_domain,
        "console_domain": settings.console_domain,
        "access_mode": settings.access_mode,
        "public_ip": settings.public_ip,
        "http_port": settings.http_port,
        "coolify_network": network,
        "compose_project": project,
        "status": "ready" if configured else "waiting_for_coolify",
        "waiting_reason": waiting_reason,
        "catalog_count": catalog_count,
        "sizing": discovered.get("sizing", {}),
        "coolify_env_file": str(paths.config_dir / "coolify.env"),
    }
    _atomic_write(paths.state_dir / "install-state.json",
                  (json.dumps(state, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    return {"release_created": created, "configured": configured, **state}


def doctor_checks(settings: Settings, discovered: Mapping[str, object]) -> list[Check]:
    checks: list[Check] = []
    release = discovered.get("os_release")
    supported = (isinstance(release, dict) and release.get("ID") == "ubuntu" and
                 release.get("VERSION_ID") in SUPPORTED_UBUNTU)
    checks.append(Check("ubuntu", "pass" if supported else "block",
                        f"{(release or {}).get('ID', 'unknown')} {(release or {}).get('VERSION_ID', 'unknown')}",
                        None if supported else "Use Ubuntu 22.04 or 24.04 on the host."))
    inside = bool(discovered.get("inside_container"))
    checks.append(Check("host-execution", "block" if inside else "pass",
                        "container detected" if inside else "running on host",
                        "Run the installer on the Docker host." if inside else None))
    docker = bool(discovered.get("docker_ready"))
    checks.append(Check("docker", "pass" if docker else "block",
                        "daemon reachable" if docker else "daemon unavailable",
                        None if docker else "Run apply as root to install/start Docker."))
    version = discovered.get("compose_version")
    try:
        compose_ok = isinstance(version, str) and parse_version(version) >= MINIMUM_COMPOSE
    except ValueError:
        compose_ok = False
    checks.append(Check("docker-compose", "pass" if compose_ok else "block",
                        str(version or "not available"),
                        None if compose_ok else "Install Docker Compose 2.33.1 or newer."))
    endpoint = discovered.get("docker_endpoint")
    endpoint_ok = endpoint == "unix:///var/run/docker.sock"
    checks.append(Check("docker-context", "pass" if endpoint_ok else "block",
                        str(endpoint or "not detected"),
                        None if endpoint_ok else "Use the local rootful Docker context and unset DOCKER_HOST."))
    cgroup = bool(discovered.get("cgroup_v2"))
    checks.append(Check("cgroup-v2", "pass" if cgroup else "block",
                        "available" if cgroup else "not detected",
                        None if cgroup else "Enable unified cgroup v2 on the Ubuntu host."))
    binder = binder_devices_ready()
    checks.append(Check("binder", "pass" if binder else "block",
                        "binder, hwbinder and vndbinder available" if binder else
                        "one or more Redroid binder devices are missing",
                        None if binder else "Load binder_linux with binder,hwbinder,vndbinder."))
    guard_ready = bool(discovered.get("docker_user_chain")) and bool(shutil.which("iptables-restore"))
    checks.append(Check("egress-guard", "pass" if guard_ready else "block",
                        ("DOCKER-USER and iptables-restore available" if guard_ready
                         else "host firewall prerequisite missing"),
                        None if guard_ready else "Start Docker and install iptables with iptables-restore."))
    network = discovered.get("coolify_network")
    network_exists = isinstance(network, str) and network in discovered.get("networks", [])
    checks.append(Check("coolify-network", "pass" if network_exists else "block",
                        str(network or "not detected"),
                        (None if network_exists
                         else "Create/select the existing Coolify proxy network and rerun doctor.")))
    conflicts = discovered.get("network_pool_conflicts")
    conflicts = conflicts if isinstance(conflicts, list) else []
    inventory_ready = bool(discovered.get("network_inventory_ready"))
    checks.append(Check(
        "address-pools", "pass" if inventory_ready and not conflicts else "block",
        ("10.231.0.0/16 and 10.232.0.0/16 are available" if inventory_ready and not conflicts
         else json.dumps(conflicts, sort_keys=True) if conflicts else
         "Docker network inventory could not be inspected"),
        (None if inventory_ready and not conflicts else
         "Remove/readdress conflicts or restore Docker network inspection, then retry.")
    ))
    project = discovered.get("compose_project")
    project_ready = bool(discovered.get("anchor_present")) and not discovered.get("project_mismatch")
    checks.append(Check("coolify-project", "pass" if project_ready else "warn",
                        str(project or "farm-anchor not deployed"),
                        None if project_ready else "Deploy the core Compose in Coolify, then rerun apply."))
    port_ready = bool(discovered.get("http_port_available"))
    http_bind = "0.0.0.0" if settings.access_mode == "ip" else "127.0.0.1"
    checks.append(Check(
        "http-gateway-port", "pass" if port_ready else "block",
        f"{http_bind}:{settings.http_port}" if port_ready else
        f"{http_bind}:{settings.http_port} is owned by an unrelated listener",
        None if port_ready else "Choose another --port and rerun apply."
    ))
    sizing = discovered.get("sizing") if isinstance(discovered.get("sizing"), dict) else {}
    limit = int(sizing.get("calculated_active_limit", 0))
    checks.append(Check("capacity", "pass" if limit > 0 else ("warn" if not docker else "block"),
                        f"active={sizing.get('active_devices', 0)} catalog={sizing.get('catalog_devices', 0)} "
                        f"active_limit={limit} additional_catalog={sizing.get('safe_additional_catalog_devices', 0)}",
                        None if limit > 0 else "Free CPU/RAM/disk or inspect the resource probe."))
    config_dir_ok, config_dir_detail = private_path_status(settings.paths.config_dir, directory=True)
    checks.append(Check("private-config-directory", "pass" if config_dir_ok else "block",
                        config_dir_detail,
                        None if config_dir_ok else "Run apply to create /etc/android-farm with mode 0700."))
    grafana_secret = settings.paths.config_dir / "monitoring" / "grafana-admin-password"
    grafana_ok, grafana_detail = private_path_status(grafana_secret)
    checks.append(Check("grafana-admin-secret", "pass" if grafana_ok else "block",
                        grafana_detail,
                        None if grafana_ok else "Run apply to create the private Grafana password file."))
    config = settings.paths.config_dir / "provisioner.json"
    config_ok, config_detail = private_path_status(config)
    if config_ok:
        env_ok, env_detail = private_path_status(settings.paths.config_dir / "compose.env")
        checks.append(Check("provisioner-config", "pass" if env_ok else "block",
                            str(config) if env_ok else f"Compose environment: {env_detail}",
                            None if env_ok else "Run apply to restore private Compose configuration."))
    else:
        missing = config_detail == "missing"
        checks.append(Check("provisioner-config", "warn" if missing else "block",
                            "not finalized" if missing else config_detail,
                            ("Run apply after farm-anchor exists." if missing
                             else "Restore root ownership and mode 0600, then rerun doctor.")))
    state_file = settings.paths.state_dir / "install-state.json"
    state_ok, state_detail = private_path_status(state_file)
    state = _read_json_if_regular(state_file) if state_ok else {}
    install_status = state.get("status")
    if state_ok and install_status == "ready":
        checks.append(Check("installer-state", "pass", "ready"))
    elif state_ok and install_status == "waiting_for_coolify":
        reason = state.get("waiting_reason")
        checks.append(Check(
            "installer-state", "warn",
            str(reason) if isinstance(reason, str) and reason else "Coolify handoff is unfinished",
            "Complete the matching Coolify deployment and rerun apply."
        ))
    else:
        missing = state_detail == "missing"
        checks.append(Check(
            "installer-state", "warn" if missing else "block",
            "not installed" if missing else
            (state_detail if not state_ok else f"invalid status: {install_status!r}"),
            "Run apply to create a finalized installer state."
        ))
    release_dir = Path(str(state.get("release_dir", ""))) if state.get("release_dir") else None
    release_is_managed = bool(
        release_dir and release_dir.is_absolute() and release_dir.parent == settings.paths.release_root and
        re.fullmatch(r"[0-9a-f]{16}", release_dir.name)
    )
    release_ok, release_detail = (verify_release_directory(release_dir) if release_is_managed
                                  else (False, "installed release path is absent or outside the managed root"))
    checks.append(Check("immutable-release", "pass" if release_ok else "warn",
                        str(release_dir) if release_ok else release_detail,
                        None if release_ok else "Run apply to stage the reviewed release."))
    if install_status == "ready" and config_ok:
        provisioner = _read_json_if_regular(config)
        compose_file = provisioner.get("compose_file")
        active_release = (Path(compose_file).parent if isinstance(compose_file, str) and
                          Path(compose_file).is_absolute() else None)
        active_ok = active_release == release_dir
        checks.append(Check(
            "active-release", "pass" if active_ok else "block",
            str(active_release) if active_ok else
            f"provisioner={active_release or 'invalid'} installer={release_dir or 'invalid'}",
            None if active_ok else "Rerun apply after all active devices are stopped."
        ))
    anchor_release = discovered.get("anchor_release")
    installed_id = state.get("release_id")
    if isinstance(anchor_release, str) and isinstance(installed_id, str) and anchor_release != installed_id:
        checks.append(Check("release-parity", "block",
                            f"Coolify={anchor_release} host={installed_id}",
                            "Deploy the host release ID in Coolify, then rerun apply."))
    elif isinstance(anchor_release, str) and isinstance(installed_id, str):
        checks.append(Check("release-parity", "pass", anchor_release))
    else:
        checks.append(Check("release-parity", "warn", "anchor has no comparable release label",
                            "Set FARM_RELEASE_ID in Coolify to the value from coolify.env."))
    dynamic = settings.paths.traefik_dynamic_dir
    middleware = dynamic / "farm-auth.yml"
    users = dynamic / "farm-users.htpasswd"
    try:
        middleware_ok = (not middleware.is_symlink() and middleware.is_file() and
                         middleware.read_bytes() ==
                         (settings.source / "traefik" / "farm-auth.yml").read_bytes())
    except OSError:
        middleware_ok = False
    checks.append(Check("traefik-auth-middleware", "pass" if middleware_ok else "block",
                        str(middleware) if middleware_ok else "missing or differs from reviewed configuration",
                        None if middleware_ok else "Run apply to install the farm-auth middleware."))
    users_ok, users_detail = private_path_status(users)
    checks.append(Check("traefik-auth-users", "pass" if users_ok else "block", users_detail,
                        None if users_ok else
                        "Run apply with --auth-user and --auth-password-file (root-owned, chmod 0600)."))
    return checks


def _settings_from_args(args: argparse.Namespace) -> Settings:
    source = args.source.resolve()
    http_port = normalize_http_port(
        args.port if args.port is not None else DEFAULT_HTTP_PORT
    )
    if args.ip:
        access_mode = "ip"
        public_ip = normalize_public_ip(args.ip)
        # Keep required domain-shaped fields deterministic without inventing DNS.
        farm_domain = public_ip
        console_domain = public_ip
    else:
        access_mode = "domain"
        public_ip = None
        farm_domain = normalize_domain(args.farm_domain)
        console_domain = normalize_domain(args.console_domain)
    network = args.coolify_network
    if network != "auto" and not PROJECT_RE.fullmatch(network):
        raise ValueError("invalid Docker network name")
    project = args.compose_project
    if project != "auto":
        validate_project(project)
    catalog = args.catalog_count
    if catalog == "auto":
        catalog = None
    else:
        try:
            catalog = int(catalog)
        except ValueError as exc:
            raise ValueError("--catalog-count must be auto or a positive integer") from exc
        from ops.device_ids import DEVICE_LIMIT
        if not 1 <= catalog <= DEVICE_LIMIT:
            raise ValueError(f"--catalog-count must be between 1 and {DEVICE_LIMIT}")
    if bool(args.auth_user) != bool(args.auth_password_file):
        raise ValueError("--auth-user and --auth-password-file must be supplied together")
    return Settings(source, farm_domain, console_domain, network, project,
                    Paths(args.release_root, args.config_dir, args.state_dir, args.backup_dir,
                          args.data_root, args.wrapper, args.traefik_dynamic_dir),
                    args.skip_host_bootstrap, catalog, args.auth_user, args.auth_password_file,
                    access_mode, public_ip, http_port)


def plan(settings: Settings) -> dict[str, object]:
    discovered = discover(settings)
    sizing = discovered.get("sizing") if isinstance(discovered.get("sizing"), dict) else {}
    catalog = choose_catalog_count(settings, sizing)
    sizing["recommended_catalog_count"] = catalog
    digest, _ = release_digest(settings.source, catalog)
    actions = [
        "prepare Ubuntu prerequisites only when missing",
        f"stage immutable release {digest[:16]} under {settings.paths.release_root}",
        f"create secure configuration under {settings.paths.config_dir}",
        f"install fail-closed Traefik authentication under {settings.paths.traefik_dynamic_dir}",
        "preserve all existing proxy secrets, APK trust policy, inventory, backups, and device data",
    ]
    if not discovered.get("compose_project"):
        actions.append("wait for farm-anchor deployment, then finalize on the next identical apply")
    else:
        actions.append("write the device-provisioner wrapper and finalized Coolify project binding")
    return {"mode": "plan", "release_id": digest[:16], "actions": actions, "detected": discovered}


def apply(settings: Settings) -> dict[str, object]:
    if platform.system() != "Linux" or os.name != "posix":
        raise RuntimeError("apply must run on the target Ubuntu host")
    if os.geteuid() != 0:
        raise RuntimeError("apply requires root")
    release = _host_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") not in SUPPORTED_UBUNTU:
        raise RuntimeError("only Ubuntu 22.04 and 24.04 are supported")
    if Path("/.dockerenv").exists():
        raise RuntimeError("run apply on the host, not inside a container")
    prepare_host(settings.source, settings.skip_host_bootstrap)
    endpoint = docker_endpoint()
    if endpoint != "unix:///var/run/docker.sock":
        raise RuntimeError("apply requires the local rootful Docker context; unset DOCKER_HOST/DOCKER_CONTEXT")
    # Re-discover under the same lock used by farmctl. Without this, an Android
    # start can race the zero-active upgrade check and the live CLI could switch
    # release trees while that old-release container remains active.
    with lifecycle_lock():
        discovered = discover(settings)
        if discovered.get("coolify_network") not in discovered.get("networks", []):
            raise RuntimeError("selected Coolify network does not exist on the local Docker host")
        conflicts = discovered.get("network_pool_conflicts")
        if not discovered.get("network_inventory_ready"):
            raise RuntimeError("Docker network inventory could not be inspected; refusing a fail-open install")
        if isinstance(conflicts, list) and conflicts:
            raise RuntimeError("Docker networks overlap reserved Android Farm pools: " +
                               json.dumps(conflicts, sort_keys=True))
        if not discovered.get("http_port_available"):
            raise RuntimeError(
                f"HTTP gateway port {settings.http_port} is already used by an unrelated listener"
            )
        auth = configure_traefik_auth(settings)
        result = install_managed_files(settings, discovered)
    return {"mode": "apply", "detected": discovered, "traefik_auth": auth,
            "coolify_compose": str(settings.source / "docker-compose.yml"),
            "result": result}


def doctor(settings: Settings) -> dict[str, object]:
    discovered = discover(settings)
    checks = doctor_checks(settings, discovered)
    status = "blocked" if any(item.status == "block" for item in checks) else (
        "action_required" if any(item.status == "warn" for item in checks) else "ready")
    return {"mode": "doctor", "status": status, "checks": [asdict(item) for item in checks],
            "detected": discovered}


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", type=Path, default=PROJECT_ROOT)
    common.add_argument("--farm-domain", default="farm.example.com")
    common.add_argument("--console-domain", default="console.farm.example.com")
    common.add_argument("--ip", help="explicit IPv4 access mode; serves authenticated HTTP")
    common.add_argument("--port", type=int,
                        help=f"IP gateway port (default {DEFAULT_HTTP_PORT}; outside ADB ports)")
    common.add_argument("--coolify-network", default="auto")
    common.add_argument("--compose-project", default="auto")
    common.add_argument("--release-root", type=Path, default=Paths.release_root)
    common.add_argument("--config-dir", type=Path, default=Paths.config_dir)
    common.add_argument("--state-dir", type=Path, default=Paths.state_dir)
    common.add_argument("--backup-dir", type=Path, default=Paths.backup_dir)
    common.add_argument("--data-root", type=Path, default=Paths.data_root)
    common.add_argument("--wrapper", type=Path, default=Paths.wrapper)
    common.add_argument("--traefik-dynamic-dir", type=Path, default=Paths.traefik_dynamic_dir)
    common.add_argument("--auth-user", help="Basic Auth operator name; use with --auth-password-file")
    common.add_argument("--auth-password-file", type=Path,
                        help="root-owned chmod 0600 one-line password file; never passed in argv")
    common.add_argument("--skip-host-bootstrap", action="store_true",
                        help="advanced: do not install/check host packages during apply")
    common.add_argument("--catalog-count", default="auto",
                        help="auto derives persistent slots from /opt/farm/data; or use a positive integer")
    common.add_argument("--json", action="store_true")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()
    sub.add_parser("plan", parents=[common], help="show detections and intended changes")
    sub.add_parser("apply", parents=[common], help="idempotently install or reconcile the host")
    sub.add_parser("doctor", parents=[common], help="report readiness and exact remediation")
    return parser


def _print_human(payload: Mapping[str, object]) -> None:
    mode = payload.get("mode")
    if mode == "doctor":
        print(f"doctor: {payload['status']}")
        for item in payload.get("checks", []):
            marker = {"pass": "OK", "warn": "WARN", "block": "BLOCK"}.get(item["status"], "?")
            print(f"[{marker}] {item['name']}: {item['detail']}")
            if item.get("remediation"):
                print(f"       {item['remediation']}")
        return
    if mode == "plan":
        print(f"release: {payload['release_id']}")
        for index, action in enumerate(payload.get("actions", []), 1):
            print(f"{index}. {action}")
        print("Next: run the same options with `apply`, including the private Basic Auth password file.")
        return
    if mode == "apply":
        result = payload.get("result", {})
        if not isinstance(result, Mapping):
            raise RuntimeError("installer returned an invalid result")
        print(f"release: {result.get('release_id', 'unknown')}")
        print(f"status: {result.get('status', 'unknown')}")
        print(f"Coolify Compose: {payload.get('coolify_compose', 'docker-compose.yml')}")
        print(f"Coolify ENV source: {result.get('coolify_env_file', 'unknown')}")
        if result.get("status") == "waiting_for_coolify":
            print("Next: import docker-compose.yml in one Coolify Compose application, copy coolify.env values, deploy, then rerun this exact apply command.")
            if result.get("waiting_reason"):
                print(f"Waiting: {result['waiting_reason']}")
        else:
            print("Next: run `python3 installer/install.py doctor` and complete the HTTPS/ADB acceptance checks in docs/GUIDE.fa.md.")
        return
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments.insert(0, "plan")
    elif arguments[0] not in {"plan", "apply", "doctor", "-h", "--help"}:
        arguments.insert(0, "plan")
    args = build_parser().parse_args(arguments)
    try:
        settings = _settings_from_args(args)
        payload = {"plan": plan, "apply": apply, "doctor": doctor}[args.command](settings)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_human(payload)
        return 2 if args.command == "doctor" and payload.get("status") == "blocked" else 0
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
