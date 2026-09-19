"""Operator intent per device: should this device be running right now?

``farmctl start`` records ``running=True`` only after the attested start
succeeded; every guarded stop (operator ``down``, safety hold, backup, failed
start) records ``running=False``.  The health controller restarts a crashed
Android container only while the intent is ``running``; an intentional stop is
therefore never undone by automation.
"""
from __future__ import annotations

from pathlib import Path
import time

try:
    from .device_ids import canonical_device
    from .secureio import atomic_json, read_private_json, require_private_directory
except ImportError:  # direct host execution
    from device_ids import canonical_device
    from secureio import atomic_json, read_private_json, require_private_directory


DEFAULT_PATH = Path('/var/lib/android-farm/desired-state.json')


def load(path=DEFAULT_PATH):
    path = Path(path)
    if not path.exists():
        return {'schema_version': 1, 'devices': {}}
    value = read_private_json(path, 'desired device state')
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or
            not isinstance(value.get('devices'), dict)):
        raise RuntimeError('desired device state file is invalid')
    return value


def set_running(device, running, *, path=DEFAULT_PATH, clock=time.time):
    device = canonical_device(device)
    path = Path(path)
    require_private_directory(path.parent, 'desired state directory', create=True)
    state = load(path)
    state['devices'][device] = {'running': bool(running), 'updated_at': int(clock())}
    atomic_json(path, state)
    return state['devices'][device]


def wants_running(device, *, path=DEFAULT_PATH):
    """True only when a successful guarded start was the last recorded intent."""
    device = canonical_device(device)
    entry = load(path)['devices'].get(device)
    return bool(isinstance(entry, dict) and entry.get('running') is True)
