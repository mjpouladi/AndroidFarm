"""Install the host-only QA control plane through the reviewed Ansible role.

The Redis credential exists only in a root-private persistent file and a
short-lived root-private Ansible vars file.  It is never placed in argv,
returned to callers, or included in an exception.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence

try:
    from .install import verify_release_directory
except ImportError:  # Direct imports from installer/ during bootstrap.
    from install import verify_release_directory


SUPPORTED_UBUNTU = frozenset({"22.04", "24.04"})
RELEASE_ROOT = Path("/opt/android-farm/releases")
REDIS_PASSWORD_FILE = Path("/etc/android-farm/redis-worker-password")
REDIS_CONFIG_FILE = Path("/etc/redis/redis.conf")
RUNTIME_DIR = Path("/run/android-farm")
_RELEASE_ID = re.compile(r"[0-9a-f]{16}\Z")
_REDIS_PASSWORD = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
Runner = Callable[..., subprocess.CompletedProcess[str]]
Verifier = Callable[[Path], tuple[bool, str]]


def _announce(message: str, fallback: str) -> None:
    """Print operator progress, retaining an ASCII fallback for restricted TTYs."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(fallback, flush=True)


def _os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _require_supported_host() -> None:
    if platform.system() != "Linux" or os.name != "posix":
        raise RuntimeError("control-plane installation must run on the Ubuntu host")
    if os.geteuid() != 0:
        raise RuntimeError("control-plane installation requires root")
    release = _os_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") not in SUPPORTED_UBUNTU:
        raise RuntimeError("only Ubuntu 22.04 and 24.04 are supported")


def _traverses_symlink(path: Path) -> bool:
    candidate = path.absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _private_directory(path: Path) -> Path:
    path = path.absolute()
    if _traverses_symlink(path):
        raise RuntimeError(f"private path may not traverse symbolic links: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"private path is not a real directory: {path}")
    path.chmod(0o700)
    if os.name == "posix":
        os.chown(path, 0, 0)
    return path


def _validate_release(
    release_dir: Path,
    *,
    release_root: Path = RELEASE_ROOT,
    verifier: Verifier = verify_release_directory,
) -> Path:
    release_dir = Path(release_dir).absolute()
    release_root = Path(release_root).absolute()
    if (_traverses_symlink(release_dir) or not release_dir.is_dir() or
            not _RELEASE_ID.fullmatch(release_dir.name)):
        raise RuntimeError("control plane requires a real immutable <16hex> release directory")
    resolved = release_dir.resolve(strict=True)
    if resolved.parent != release_root.resolve(strict=True):
        raise RuntimeError("release directory is outside the managed immutable release root")
    valid, detail = verifier(resolved)
    if not valid:
        raise RuntimeError(f"immutable release verification failed: {detail}")
    required = (
        resolved / "ansible" / "site.yml",
        resolved / "ansible" / "roles" / "android_farm" / "tasks" / "main.yml",
        resolved / "services" / "worker" / "requirements.txt",
    )
    if any(item.is_symlink() or not item.is_file() for item in required):
        raise RuntimeError("immutable release is missing the reviewed control-plane role")
    return resolved


def _run_quiet(
    runner: Runner,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: int = 1800,
    stage: str = "control-plane command",
) -> None:
    completed = runner(
        list(argv),
        check=False,
        text=True,
        capture_output=True,
        env=dict(env) if env is not None else None,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{stage} failed (exit {completed.returncode})")


def _ensure_ansible(*, runner: Runner = subprocess.run) -> str:
    executable = shutil.which("ansible-playbook")
    if executable:
        return executable
    environment = dict(os.environ)
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    _announce(
        "  نصب وابستگی Ansible از بستهٔ رسمی Ubuntu…",
        "  Installing Ansible from the official Ubuntu package...",
    )
    _run_quiet(
        runner,
        ["apt-get", "update"],
        env=environment,
        stage="Ubuntu package index refresh",
    )
    _run_quiet(
        runner,
        ["apt-get", "install", "-y", "--no-install-recommends", "ansible-core"],
        env=environment,
        stage="Ubuntu ansible-core installation",
    )
    executable = shutil.which("ansible-playbook")
    if not executable:
        raise RuntimeError("Ubuntu ansible-core installation did not provide ansible-playbook")
    return executable


def _read_private_password(path: Path) -> str:
    path = path.absolute()
    if _traverses_symlink(path) or not path.is_file():
        raise RuntimeError("Redis password must be a root-private regular file")
    info = path.stat()
    if info.st_size > 256:
        raise RuntimeError("Redis password file is unexpectedly large")
    if os.name == "posix" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError("Redis password must be root-owned and mode 0600")
    value = path.read_text(encoding="ascii")
    if value.endswith("\n"):
        value = value[:-1]
    if "\n" in value or "\r" in value:
        raise RuntimeError("Redis password file must contain exactly one line")
    if not _REDIS_PASSWORD.fullmatch(value):
        raise RuntimeError("Redis password file has an invalid value")
    return value


def _redis_password(path: Path = REDIS_PASSWORD_FILE) -> str:
    path = path.absolute()
    _private_directory(path.parent)
    if path.exists() or path.is_symlink():
        return _read_private_password(path)
    value = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_private_password(path)
    try:
        os.write(descriptor, (value + "\n").encode("ascii"))
        os.fsync(descriptor)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        if os.name == "posix" and hasattr(os, "fchown"):
            os.fchown(descriptor, 0, 0)
    finally:
        os.close(descriptor)
    return _read_private_password(path)


def _guard_unmanaged_redis(
    password_file: Path,
    *,
    redis_config: Path = REDIS_CONFIG_FILE,
    runner: Runner = subprocess.run,
) -> None:
    """Refuse to claim an existing Redis that lacks this installer's marker."""
    password_file = password_file.absolute()
    if password_file.exists() or password_file.is_symlink():
        _read_private_password(password_file)
        return
    executable_exists = shutil.which("redis-server") is not None
    config_exists = redis_config.exists() or redis_config.is_symlink()
    try:
        active = runner(
            ["systemctl", "is-active", "--quiet", "redis-server.service"],
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        ).returncode == 0
    except OSError as exc:
        raise RuntimeError("systemd is required to verify Redis ownership safely") from exc
    if executable_exists or config_exists or active:
        raise RuntimeError(
            "an existing unmanaged Redis installation was detected; it was not changed. "
            "Use the advanced external Redis path or migrate it explicitly."
        )


def _private_json(directory: Path, prefix: str, payload: Mapping[str, object]) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=directory)
    path = Path(name)
    complete = False
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        if os.name == "posix" and hasattr(os, "fchown"):
            os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        complete = True
        return path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not complete:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _private_ansible_config(directory: Path) -> Path:
    """Create a minimal private config so host/user Ansible config is ignored."""
    descriptor, name = tempfile.mkstemp(prefix="ansible-", suffix=".cfg", dir=directory)
    path = Path(name)
    complete = False
    content = (
        "[defaults]\n"
        "host_key_checking = False\n"
        "retry_files_enabled = False\n"
        "display_args_to_stdout = False\n"
        "nocows = True\n"
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        if os.name == "posix" and hasattr(os, "fchown"):
            os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        complete = True
        return path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not complete:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def install_control_plane(
    release_dir: Path | str,
    *,
    runner: Runner = subprocess.run,
    release_root: Path = RELEASE_ROOT,
    password_file: Path = REDIS_PASSWORD_FILE,
    redis_config: Path = REDIS_CONFIG_FILE,
    runtime_dir: Path = RUNTIME_DIR,
    verifier: Verifier = verify_release_directory,
) -> dict[str, object]:
    """Apply the existing Ansible role locally with an empty QA device list.

    The returned state is ``ready`` only after every managed control-plane
    unit is both active and enabled.
    """
    _require_supported_host()
    release = _validate_release(Path(release_dir), release_root=release_root, verifier=verifier)
    _guard_unmanaged_redis(Path(password_file), redis_config=Path(redis_config), runner=runner)
    ansible_playbook = _ensure_ansible(runner=runner)
    password = _redis_password(Path(password_file))
    private_runtime = _private_directory(Path(runtime_dir))
    inventory = {
        "all": {
            "children": {
                "android_farm_hosts": {
                    "hosts": {
                        "localhost": {
                            "ansible_connection": "local",
                            "ansible_become": False,
                            "ansible_python_interpreter": "/usr/bin/python3",
                        }
                    }
                }
            }
        }
    }
    variables = {
        "farm_release_dir": str(release),
        "farm_manage_redis": True,
        "farm_redis_worker_password": password,
        "farm_redis_url": "",
        "farm_devices": [],
        "farm_worker_enabled": True,
        "farm_health_enabled": True,
        "farm_fail_on_device_drift": True,
    }
    inventory_file: Path | None = None
    variables_file: Path | None = None
    ansible_config: Path | None = None
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "ANSIBLE_DISPLAY_ARGS_TO_STDOUT": "False",
        "ANSIBLE_ROLES_PATH": str(release / "ansible" / "roles"),
    }
    try:
        inventory_file = _private_json(private_runtime, "inventory-", inventory)
        variables_file = _private_json(private_runtime, "vars-", variables)
        ansible_config = _private_ansible_config(private_runtime)
        environment["ANSIBLE_CONFIG"] = str(ansible_config)
        _announce(
            "  اعمال نقش کنترل‌پلین محلی با Ansible…",
            "  Applying the local control-plane role with Ansible...",
        )
        _run_quiet(
            runner,
            [
                ansible_playbook,
                "--inventory", str(inventory_file),
                "--limit", "localhost",
                "--forks", "1",
                "--extra-vars", "@" + str(variables_file),
                str(release / "ansible" / "site.yml"),
            ],
            env=environment,
            stage="Ansible control-plane role application",
        )
        units = (
            "redis-server.service",
            "android-farm-worker.service",
            "android-farm-health.timer",
        )
        _announce(
            "  بررسی سرویس‌های کنترل‌پلین…",
            "  Validating control-plane services...",
        )
        for unit in units:
            _run_quiet(runner, ["systemctl", "is-active", "--quiet", unit],
                       env=environment, timeout=30,
                       stage=f"active-state validation for {unit}")
            _run_quiet(runner, ["systemctl", "is-enabled", "--quiet", unit],
                       env=environment, timeout=30,
                       stage=f"enabled-state validation for {unit}")
    finally:
        for temporary in (variables_file, inventory_file, ansible_config):
            if temporary is None:
                continue
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return {
        "status": "ready",
        "release_dir": str(release),
        "managed_redis": True,
        "devices_declared": 0,
        "services_validated": True,
    }
