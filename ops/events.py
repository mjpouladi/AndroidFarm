"""Durable, bounded device event log (device ready / crashed / recovered / ...).

Events are appended as JSON lines to a root-private file so the console, the
CLI and the health controller share one timeline.  Recording is best-effort
for callers that must not fail because of the log (``note``), and strict for
readers: only documented kinds and canonical device identifiers are returned.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

try:
    from .device_ids import device_id, device_index
    from .secureio import require_private_directory
except ImportError:  # direct host execution
    from device_ids import device_id, device_index
    from secureio import require_private_directory


DEFAULT_PATH = Path('/var/lib/android-farm/events.jsonl')
# Tests and isolated tooling point the log elsewhere; production never sets this.
PATH_ENVIRONMENT = 'ANDROID_FARM_EVENT_LOG'
KINDS = frozenset({
    'device-started', 'device-stopped', 'device-ready', 'device-crashed', 'device-stalled',
    'recovery-started', 'recovery-succeeded', 'recovery-failed', 'recovery-skipped',
    'device-held', 'device-released', 'provisioning-completed', 'provisioning-failed',
    'environment-applied', 'artifact-imported', 'artifact-removed',
})
MAX_LINES = 5000
KEEP_LINES = 2000
MAX_DETAIL = 200


def _strict_device(value):
    """Only canonical numNN identifiers; the devNN alias is not a log identity."""
    return device_id(device_index(value, aliases=False))


def _clean_detail(detail):
    if detail is None:
        return None
    text = str(detail).replace('\n', ' ').replace('\r', ' ')
    text = ''.join(character for character in text if ord(character) >= 32 and ord(character) != 127)
    return text[:MAX_DETAIL] or None


def default_path():
    override = os.environ.get(PATH_ENVIRONMENT)
    return Path(override) if override else DEFAULT_PATH


def record(kind, device=None, detail=None, *, path=None, clock=time.time):
    """Append one validated event; rotate the file when it grows past MAX_LINES."""
    if kind not in KINDS:
        raise ValueError(f'unknown event kind: {kind}')
    if device is not None:
        device = _strict_device(device)
    path = Path(path) if path is not None else default_path()
    require_private_directory(path.parent, 'event log directory', create=True)
    event = {'at': int(clock()), 'kind': kind, 'device': device, 'detail': _clean_detail(detail)}
    line = json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
    import fcntl
    lock = os.open(path.parent / '.events.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line.encode('utf-8'))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _rotate(path)
    finally:
        os.close(lock)
    return event


def note(kind, device=None, detail=None, *, path=None):
    """Best-effort record: a broken log must never abort a device operation."""
    try:
        return record(kind, device, detail, path=path)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'event log unavailable: {exc}', file=sys.stderr)
        return None


def _rotate(path):
    with path.open('rb') as stream:
        lines = stream.readlines()
    if len(lines) <= MAX_LINES:
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.events.', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.writelines(lines[-KEEP_LINES:])
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recent(limit=100, *, path=None, device=None):
    """Return the newest validated events first; malformed lines are skipped."""
    path = Path(path) if path is not None else default_path()
    if not 1 <= int(limit) <= KEEP_LINES:
        raise ValueError('limit must be between 1 and 2000')
    if not path.exists() or path.is_symlink() or not path.is_file():
        return []
    if os.name == 'posix':
        info = path.lstat()
        owner = 0 if os.geteuid() == 0 else os.geteuid()
        if info.st_uid != owner or info.st_mode & 0o077:
            raise RuntimeError('event log must be private to the farm operator')
    if device is not None:
        device = _strict_device(device)
    events = []
    with path.open('rb') as stream:
        lines = stream.readlines()
    for raw in reversed(lines):
        try:
            value = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (not isinstance(value, dict) or value.get('kind') not in KINDS or
                isinstance(value.get('at'), bool) or not isinstance(value.get('at'), int)):
            continue
        item_device = value.get('device')
        if item_device is not None:
            try:
                item_device = _strict_device(item_device)
            except ValueError:
                continue
        if device is not None and item_device != device:
            continue
        events.append({'at': value['at'], 'kind': value['kind'], 'device': item_device,
                       'detail': _clean_detail(value.get('detail'))})
        if len(events) >= limit:
            break
    return events
