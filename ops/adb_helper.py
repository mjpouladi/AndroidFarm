"""Bounded ADB primitives for a managed device, executed from its screen container.

Every command is a fixed argv vector (never shell text) that reaches the
Android guest only through the device's private control network.  This module
also applies the environment part of a QA profile after boot: the timezone and
locale are ``persist`` properties, so they survive restarts and are re-checked,
not re-applied, on every guarded start.
"""
from __future__ import annotations

import re
import subprocess
import time

try:
    from .device_ids import device_index, network_plan
except ImportError:  # direct host execution
    from device_ids import device_index, network_plan


PROPERTY_RE = re.compile(r'[a-z][a-z0-9_.]{1,62}\Z')
VALUE_RE = re.compile(r'[A-Za-z0-9_+./:-]{1,91}\Z')
FRAMEWORK_RESTART_TIMEOUT = 180


def output(*argv, timeout=60):
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def target(device):
    """The guest's ADB endpoint inside the device's internal control network."""
    return f"{network_plan(device_index(device, aliases=False))['proxy_control_ip']}:5555"


def command(device, *arguments):
    """docker exec into the screen container and address the guest by serial."""
    return ['docker', 'exec', f'screen-{device}', 'adb', '-s', target(device), *arguments]


def shell(device, *arguments, runner=output, timeout=20):
    return runner(*command(device, 'shell', *arguments), timeout=timeout).strip()


def getprop(device, name, runner=output):
    if not PROPERTY_RE.fullmatch(name):
        raise ValueError('invalid Android property name')
    return shell(device, 'getprop', name, runner=runner)


def setprop(device, name, value, runner=output):
    """Set one property with validated tokens; the guest never sees shell text."""
    if not PROPERTY_RE.fullmatch(name):
        raise ValueError('invalid Android property name')
    if not isinstance(value, str) or not VALUE_RE.fullmatch(value):
        raise ValueError('invalid Android property value')
    shell(device, 'setprop', name, value, runner=runner)
    observed = getprop(device, name, runner=runner)
    if observed != value:
        raise RuntimeError(f'Android did not accept property {name}')
    return observed


def wait_for_boot(device, timeout=300, runner=output, clock=time.monotonic, sleep=time.sleep):
    """Block until ``sys.boot_completed`` is 1 or raise after the deadline."""
    deadline = clock() + timeout
    while True:
        try:
            if shell(device, 'getprop', 'sys.boot_completed', runner=runner) == '1':
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
        if clock() >= deadline:
            raise RuntimeError('Android boot timed out')
        sleep(3)


def wait_for_framework(device, timeout=FRAMEWORK_RESTART_TIMEOUT, runner=output,
                       clock=time.monotonic, sleep=time.sleep):
    """After a zygote restart, wait until the activity manager answers again."""
    deadline = clock() + timeout
    while True:
        try:
            if shell(device, 'service', 'check', 'activity', runner=runner).endswith(': found'):
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
        if clock() >= deadline:
            raise RuntimeError('Android framework did not return after the environment change')
        sleep(3)


def apply_environment(device, profile, runner=output, clock=time.monotonic, sleep=time.sleep,
                      include_locale=False):
    """Apply the profile's timezone (and optionally locale); report what changed.

    The properties persist in /data, so an unchanged profile is a no-op.  A real
    change restarts only the Android framework (zygote), never the container.
    The locale already reaches Android at boot through ``ro.product.locale``;
    it is rewritten as a persisted property only when a caller asks for it.
    """
    wanted = {}
    timezone = getattr(profile, 'timezone', None)
    locale = getattr(profile, 'locale', None)
    if timezone:
        wanted['persist.sys.timezone'] = timezone
    if locale and include_locale:
        wanted['persist.sys.locale'] = locale
    changed = {}
    for name, value in wanted.items():
        if getprop(device, name, runner=runner) != value:
            setprop(device, name, value, runner=runner)
            changed[name] = value
    if 'persist.sys.timezone' in changed:
        # Keep the operator's zone: automatic time zone would revert it.
        try:
            shell(device, 'settings', 'put', 'global', 'auto_time_zone', '0', runner=runner)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    if changed:
        shell(device, 'setprop', 'ctl.restart', 'zygote', runner=runner)
        wait_for_framework(device, runner=runner, clock=clock, sleep=sleep)
    return {'applied': changed, 'verified': wanted}
