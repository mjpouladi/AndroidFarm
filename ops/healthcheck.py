#!/usr/bin/env python3
"""Bounded ADB health monitor and single-service recovery controller.

Stopped devices are intentional and are never started.  For an active device,
the monitor checks the proxy, screen-side ADB client, Android shell and boot
property.  A stalled service is restarted only after the boot grace period and
per-device exponential cooldown.  Persistent /data is never removed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable

try:
    from . import desired_state, events
    from .device_ids import device_index, network_plan
    from .secureio import read_private_json, require_private_file, require_trusted_release_file
except ImportError:
    import desired_state
    import events
    from device_ids import device_index, network_plan
    from secureio import read_private_json, require_private_file, require_trusted_release_file


DEVICE_RE = re.compile(r"num[0-9]{2,6}\Z")
PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
DEFAULT_CONFIG = Path("/etc/android-farm/provisioner.json")
DEFAULT_STATE = Path("/var/lib/android-farm/health-state.json")
DEFAULT_METRICS = Path("/var/lib/node_exporter/textfile_collector/android_farm_health.prom")
RECOVERY_SKIPPED_MARKER = "ANDROID_FARM_RECOVERY_SKIPPED "
RECOVERY_TIMEOUT = 3300
ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=timeout, shell=False)


def probe_command(argv: list[str], runner: Callable, timeout: int = 20) -> subprocess.CompletedProcess:
    """An unresponsive ADB/proxy is a failed probe, not a failed fleet sweep."""
    try:
        return runner(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 124, "", "probe did not complete")


def canonical_device(value: str) -> str:
    if not isinstance(value, str) or not DEVICE_RE.fullmatch(value):
        raise ValueError("device must be a canonical numXX identifier")
    device_index(value, aliases=False)
    return value


def load_control_config(path: Path) -> dict:
    value = read_private_json(path, "health controller config")
    required = {"compose_file", "compose_project"}
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("provisioner config is missing Compose settings")
    compose = require_trusted_release_file(Path(value["compose_file"]), "health Compose release")
    project = value["compose_project"]
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        raise ValueError("invalid compose_project")
    env = (require_private_file(Path(value["compose_env_file"]), "health Compose environment")
           if value.get("compose_env_file") else None)
    access_mode = value.get("access_mode", "domain")
    if access_mode not in {"domain", "ip"}:
        raise ValueError("invalid access_mode")
    return {"compose_file": compose, "compose_project": project, "compose_env_file": env,
            "access_mode": access_mode,
            "secret_dir": Path(value.get("secret_dir", "/etc/android-farm/secrets")),
            "profile_dir": Path(value.get("profile_dir", "/etc/android-farm/device-profiles")),
            "proxy_registry": Path(value.get("proxy_registry", "/var/lib/android-farm/proxies.json")),
            "proxy_store_dir": Path(value.get("proxy_store_dir", "/etc/android-farm/proxies"))}


RECOVERY_ACTIONS = {"android": "recover", "screen": "recover-screen", "android-crashed": "recover-crashed"}


def compose_argv(config: dict, action: str, component: str, device: str) -> list[str]:
    if action != "restart" or component not in RECOVERY_ACTIONS:
        raise ValueError("unsupported recovery operation")
    canonical_device(device)
    # Android recovery must traverse farmctl's full identity/egress attestation
    # path. Screen recovery starts only the already-existing container. A
    # crashed Android (exited while the operator intent is "running") goes
    # through the same attested start. Every action re-checks the live state
    # while holding the shared lifecycle lock, closing the observation race.
    recovery_action = RECOVERY_ACTIONS[component]
    argv = [sys.executable, "-m", "ops.farmctl", recovery_action, device]
    if config.get("compose_env_file"):
        argv.extend(["--env-file", str(config["compose_env_file"])])
    argv.extend(["--project", config["compose_project"], "--compose", str(config["compose_file"]),
                 "--access-mode", config.get("access_mode", "domain"),
                 "--secret-dir", str(config["secret_dir"]),
                 "--profile-dir", str(config["profile_dir"]),
                 "--proxy-registry", str(config["proxy_registry"]),
                 "--proxy-store-dir", str(config["proxy_store_dir"])])
    return argv


@contextmanager
def health_state_lock(path: Path = Path("/run/lock/android-farm-health.lock")):
    """Serialize timer and queued health runs so state updates are not lost."""
    if os.name != "posix":
        raise RuntimeError("health recovery is supported only on the Linux host")
    import fcntl
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _inspect(name: str, runner: Callable = run) -> dict | None:
    result = runner(["docker", "inspect", name], timeout=20)
    if result.returncode:
        if any(message in (result.stderr or "").lower() for message in ("no such object", "no such container")):
            return None
        raise RuntimeError("Docker inspection failed")
    try:
        value = json.loads(result.stdout)
        return value[0] if isinstance(value, list) and value else None
    except (json.JSONDecodeError, IndexError, TypeError):
        return None


def _running(item: dict | None) -> bool:
    return bool(item and item.get("State", {}).get("Running"))


def _age_seconds(item: dict | None, now: float) -> float:
    started = (item or {}).get("State", {}).get("StartedAt")
    if not started:
        return 0
    try:
        parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
        return max(0.0, now - parsed.astimezone(timezone.utc).timestamp())
    except (ValueError, TypeError):
        return 0


@dataclass(frozen=True)
class Observation:
    device: str
    state: str
    android_running: bool
    screen_running: bool
    proxy_running: bool
    adb_healthy: bool
    boot_completed: bool
    proxy_healthy: bool
    proxy_latency_seconds: float
    recovery_component: str | None = None
    probe_failed: bool = False


def _crashed(item: dict | None) -> bool:
    """An existing, exited Android container that did not stop cleanly."""
    if not item:
        return False
    state = item.get("State") or {}
    if state.get("Running") or state.get("Paused") or state.get("Restarting"):
        return False
    exit_code = state.get("ExitCode")
    return bool(state.get("OOMKilled")) or (isinstance(exit_code, int) and exit_code != 0)


def observe(device: str, runner: Callable = run, clock: Callable[[], float] = time.time,
            boot_grace: int = 600, wants_running: Callable[[str], bool] | None = None) -> Observation:
    canonical_device(device)
    try:
        return _observe(device, runner, clock, boot_grace,
                        wants_running if wants_running is not None else desired_state.wants_running)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        # Docker itself is unavailable: do not infer an intentional stop or
        # attempt recovery from an unknown container state.
        return Observation(device, "probe_failed", False, False, False,
                           False, False, False, 0.0, probe_failed=True)


def _observe(device: str, runner: Callable, clock: Callable[[], float],
             boot_grace: int, wants_running: Callable[[str], bool]) -> Observation:
    device = canonical_device(device)
    now = clock()
    android = _inspect(f"android-{device}", runner)
    proxy = _inspect(f"proxy-{device}", runner)
    screen = _inspect(f"screen-{device}", runner)
    android_running, proxy_running, screen_running = map(_running, (android, proxy, screen))
    if not android_running:
        # A stop is intentional unless the last recorded intent was a successful
        # start and the container itself exited abnormally: that is a crash.
        if _crashed(android) and wants_running(device):
            return Observation(device, "crashed", False, screen_running, proxy_running,
                               False, False, False, 0.0, "android-crashed")
        return Observation(device, "stopped", False, screen_running, proxy_running,
                           False, False, False, 0.0)

    proxy_started = time.monotonic()
    proxy_result = probe_command(["docker", "exec", f"proxy-{device}", "/healthcheck.sh"], runner) if proxy_running else None
    proxy_latency = max(0.0, time.monotonic() - proxy_started)
    proxy_healthy = bool(proxy_result and proxy_result.returncode == 0)
    if not screen_running:
        # Recreate is deliberately not automatic: a missing screen container
        # may require the protected per-device profile override. A stopped,
        # existing screen can be restarted without changing its configuration.
        return Observation(device, "screen_missing" if screen is None else "stalled",
                           True, False, proxy_running, False, False,
                           proxy_healthy, proxy_latency, None if screen is None else "screen")

    target = f"{network_plan(device_index(device, aliases=False))['proxy_control_ip']}:5555"
    adb = ["docker", "exec", f"screen-{device}", "adb", "-s", target]
    shell = probe_command([*adb, "shell", "echo", "android-farm-health"], runner)
    boot = probe_command([*adb, "shell", "getprop", "sys.boot_completed"], runner) if shell.returncode == 0 else None
    adb_healthy = shell.returncode == 0 and shell.stdout.strip() == "android-farm-health"
    boot_completed = bool(boot and boot.returncode == 0 and boot.stdout.strip() == "1")
    if adb_healthy and boot_completed and proxy_healthy:
        state = "healthy"
        component = None
    elif not proxy_healthy:
        state = "proxy_unhealthy"
        component = None
    elif not boot_completed and _age_seconds(android, now) < boot_grace:
        state = "booting"
        component = None
    else:
        state = "stalled"
        component = "android"
    return Observation(device, state, True, True, proxy_running, adb_healthy,
                       boot_completed, proxy_healthy, proxy_latency, component)


def discover(runner: Callable = run) -> list[str]:
    result = runner(["docker", "ps", "-a", "--filter", "label=farm.role=android",
                     "--format", '{{.Label "farm.device"}}'], timeout=30)
    if result.returncode:
        raise RuntimeError("failed to discover managed Android containers")
    return sorted({canonical_device(line.strip()) for line in result.stdout.splitlines() if line.strip()})


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "devices": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("devices"), dict):
        raise ValueError("invalid health state file")
    return value


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prometheus(observations: list[Observation], state: dict, run_deadline: int = 0) -> str:
    lines = [
        "# HELP android_farm_adb_healthy Whether ADB shell and boot completion are healthy.",
        "# TYPE android_farm_adb_healthy gauge",
        "# HELP android_farm_android_running Whether the managed Android container is running.",
        "# TYPE android_farm_android_running gauge",
        "# HELP android_farm_screen_running Whether the managed browser-control container is running.",
        "# TYPE android_farm_screen_running gauge",
        "# HELP android_farm_proxy_healthcheck_duration_seconds Proxy healthcheck latency.",
        "# TYPE android_farm_proxy_healthcheck_duration_seconds gauge",
        "# HELP android_farm_proxy_healthy Whether the device proxy healthcheck succeeded.",
        "# TYPE android_farm_proxy_healthy gauge",
        "# HELP android_farm_health_restarts_total Recovery restarts performed by the health controller.",
        "# TYPE android_farm_health_restarts_total counter",
        "# HELP android_farm_health_recovery_failures_total Failed bounded recovery commands.",
        "# TYPE android_farm_health_recovery_failures_total counter",
        "# HELP android_farm_health_run_deadline_seconds Deadline of an active bounded recovery run, or zero.",
        "# TYPE android_farm_health_run_deadline_seconds gauge",
        f"android_farm_health_run_deadline_seconds {run_deadline}",
        "# HELP android_farm_probe_failed Whether Docker inspection failed and device state is unknown.",
        "# TYPE android_farm_probe_failed gauge",
        "# HELP android_farm_android_crashed Whether the Android container exited abnormally while the operator intent is running.",
        "# TYPE android_farm_android_crashed gauge",
    ]
    for item in observations:
        label = f'device="{item.device}"'
        lines.append(f"android_farm_probe_failed{{{label}}} {1 if item.probe_failed else 0}")
        lines.append(f"android_farm_android_crashed{{{label}}} {1 if item.state == 'crashed' else 0}")
        lines.append(f"android_farm_adb_healthy{{{label}}} {1 if item.adb_healthy and item.boot_completed else 0}")
        lines.append(f"android_farm_android_running{{{label}}} {1 if item.android_running else 0}")
        lines.append(f"android_farm_screen_running{{{label}}} {1 if item.screen_running else 0}")
        lines.append(f"android_farm_proxy_healthcheck_duration_seconds{{{label}}} {item.proxy_latency_seconds:.6f}")
        lines.append(f"android_farm_proxy_healthy{{{label}}} {1 if item.proxy_healthy else 0}")
        restarts = state.get("devices", {}).get(item.device, {}).get("restart_total", 0)
        lines.append(f"android_farm_health_restarts_total{{{label}}} {int(restarts)}")
        failures = state.get("devices", {}).get(item.device, {}).get("recovery_failure_total", 0)
        lines.append(f"android_farm_health_recovery_failures_total{{{label}}} {int(failures)}")
    return "\n".join(lines) + "\n"


def reconcile(observations: list[Observation], config: dict, state: dict, runner: Callable = run,
              clock: Callable[[], float] = time.time, dry_run: bool = False,
              cooldown_base: int = 300, cooldown_max: int = 3600,
              restart_limit: int = 1, failure_threshold: int = 2,
              notify: Callable = events.note) -> list[dict]:
    now = int(clock())
    actions = []
    restarted = 0
    devices = state.setdefault("devices", {})
    for item in observations:
        previous = devices.get(item.device, {})
        record = dict(previous)
        if item.state == "healthy":
            if previous.get("last_state") not in (None, "healthy"):
                notify("device-ready", item.device, f"healthy after {previous.get('last_state')}")
            record.update(consecutive_failures=0, last_state=item.state, checked_at=now)
        elif item.state in {"stopped", "booting", "proxy_unhealthy", "probe_failed"}:
            # These states are not evidence of a repeated ADB/screen stall.
            # Reset the stall counter so an unrelated condition cannot make a
            # later single failure cross the recovery threshold.
            record.update(consecutive_failures=0, last_state=item.state, checked_at=now)
        else:
            failures = int(previous.get("consecutive_failures", 0)) + 1
            if previous.get("last_state") != item.state:
                notify("device-crashed" if item.state == "crashed" else "device-stalled", item.device,
                       "container exited abnormally" if item.state == "crashed" else
                       f"{item.recovery_component or 'device'} unresponsive")
            record.update(consecutive_failures=failures, last_state=item.state, checked_at=now)
            allowed_at = int(previous.get("next_restart_at", 0))
            # A crash and a stopped-but-existing screen are restarted on first
            # sight; ADB stalls must repeat before recovery. The exponential
            # cooldown below applies to every kind (300 s, 600 s, ... 3600 s).
            immediate = item.state == "crashed" or (item.recovery_component == "screen" and not item.screen_running)
            threshold = 1 if immediate else failure_threshold
            if (item.recovery_component and failures >= threshold and now >= allowed_at and
                    restarted < restart_limit):
                action = "restart"
                argv = compose_argv(config, action, item.recovery_component, item.device)
                actions.append({"device": item.device, "component": item.recovery_component,
                                "action": action, "dry_run": dry_run})
                restarted += 1
                if not dry_run:
                    delay = min(cooldown_max, cooldown_base * (2 ** min(failures - 1, 8)))
                    notify("recovery-started", item.device, f"{item.recovery_component} attempt {failures}")
                    try:
                        result = runner(argv, timeout=RECOVERY_TIMEOUT)
                        succeeded = result.returncode == 0
                        skipped = succeeded and RECOVERY_SKIPPED_MARKER in (result.stdout or "")
                    except (OSError, subprocess.SubprocessError):
                        succeeded = False
                        skipped = False
                    actions[-1]["succeeded"] = succeeded
                    actions[-1]["skipped"] = skipped
                    if skipped:
                        # The locked farmctl re-check observed an intentional
                        # stop or another recovery already completed the work.
                        notify("recovery-skipped", item.device, "state changed after observation")
                        record.update(consecutive_failures=0,
                                      last_state="changed_after_observation")
                    elif succeeded:
                        notify("recovery-succeeded", item.device, f"next attempt not before {delay}s")
                        record["last_restart_at"] = now
                        record["next_restart_at"] = now + delay
                        record["restart_total"] = int(previous.get("restart_total", 0)) + 1
                        record.pop("last_recovery_error", None)
                    else:
                        notify("recovery-failed", item.device, f"cooldown {delay}s")
                        record["last_restart_at"] = now
                        record["next_restart_at"] = now + delay
                        record["recovery_failure_total"] = int(
                            previous.get("recovery_failure_total", 0)) + 1
                        record["last_recovery_error"] = "managed recovery command failed"
        devices[item.device] = record
    return actions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--device", action="append", default=[])
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--metrics-file", type=Path, default=DEFAULT_METRICS)
    # Matches the first-boot budget of the attested start; a boot inside it is never a stall.
    parser.add_argument("--boot-grace", type=int, default=600)
    parser.add_argument("--cooldown-base", type=int, default=300)
    parser.add_argument("--cooldown-max", type=int, default=3600)
    parser.add_argument("--restart-limit", type=int, default=1)
    parser.add_argument("--failure-threshold", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.restart_limit <= 10:
        raise ValueError("restart-limit must be between 0 and 10")
    if not 1 <= args.failure_threshold <= 5:
        raise ValueError("failure-threshold must be between 1 and 5")
    if not 30 <= args.cooldown_base <= args.cooldown_max <= 86400:
        raise ValueError("invalid cooldown bounds")
    config = load_control_config(args.config)
    with health_state_lock():
        devices = [canonical_device(value) for value in args.device] or discover()
        observations = [observe(device, boot_grace=args.boot_grace) for device in devices]
        state = read_state(args.state_file)
        # Publish observations before a potentially cold image build. The bounded
        # deadline lets monitoring distinguish active recovery from a dead timer.
        deadline = int(time.time()) + RECOVERY_TIMEOUT * args.restart_limit + 60
        atomic_write(args.metrics_file, prometheus(observations, state, deadline), 0o644)
        actions = reconcile(observations, config, state, dry_run=args.dry_run,
                            cooldown_base=args.cooldown_base, cooldown_max=args.cooldown_max,
                            restart_limit=args.restart_limit, failure_threshold=args.failure_threshold)
        if not args.dry_run:
            atomic_write(args.state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")
        atomic_write(args.metrics_file, prometheus(observations, state), 0o644)
    print(json.dumps({"devices": [item.__dict__ for item in observations], "actions": actions},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
