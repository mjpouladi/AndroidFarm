"""Versioned, monotonic device inventory."""
from pathlib import Path

try:
    from .device_ids import DEVICE_LIMIT, device_id, device_index
    from .secureio import atomic_json, read_private_json
except ImportError:  # direct host execution
    from device_ids import DEVICE_LIMIT, device_id, device_index
    from secureio import atomic_json, read_private_json

SCHEMA_VERSION = 2
COMPLETE_PHASES = frozenset({'ready_for_operator', 'ready_for_manual_registration'})


def empty_inventory():
    return {'schema_version': SCHEMA_VERSION, 'next_index': 1, 'devices': {}}


def normalize(raw):
    # Read-only compatibility with the first list-based format; the next save migrates it.
    if isinstance(raw, list):
        devices = {item['id']: item for item in raw}
        maximum = max((device_index(device, aliases=False) for device in devices), default=0)
        raw = {'schema_version': SCHEMA_VERSION, 'next_index': maximum + 1, 'devices': devices}
    if not isinstance(raw, dict) or raw.get('schema_version') != SCHEMA_VERSION:
        raise RuntimeError(f'inventory schema must be version {SCHEMA_VERSION}')
    devices = raw.get('devices')
    next_index = raw.get('next_index')
    if not isinstance(devices, dict) or isinstance(next_index, bool) or not isinstance(next_index, int):
        raise RuntimeError('inventory structure is invalid')
    seen_phone = set()
    seen_proxy = set()
    maximum = 0
    for device, record in devices.items():
        try:
            canonical = device_id(device_index(device, aliases=False))
        except ValueError as exc:
            raise RuntimeError('inventory contains an invalid device record') from exc
        if canonical != device or not isinstance(record, dict) or record.get('id') != device:
            raise RuntimeError('inventory contains an invalid device record')
        maximum = max(maximum, int(device[3:]))
        for key, seen in (('phone_hash', seen_phone), ('proxy_hash', seen_proxy)):
            value = record.get(key)
            if not isinstance(value, str) or not value or value in seen:
                raise RuntimeError(f'inventory contains an invalid or duplicate {key}')
            seen.add(value)
    if not maximum < next_index <= DEVICE_LIMIT + 1:
        raise RuntimeError('inventory next_index is not monotonic')
    return raw


def load(path=Path('/var/lib/android-farm/inventory.json')):
    path = Path(path)
    if not path.exists():
        return empty_inventory()
    return normalize(read_private_json(path, 'inventory'))


def save(path, inventory):
    atomic_json(path, normalize(inventory))


def records(inventory):
    return list(inventory['devices'].values())


def find(inventory, device):
    return inventory['devices'].get(device)


def choose(inventory, phone_hash, proxy_hash, request_hash):
    for record in records(inventory):
        if record['phone_hash'] == phone_hash:
            if record['request_hash'] != request_hash:
                raise RuntimeError('existing phone has a different request; do not reassign its identity')
            return record, False
    # ``ready_for_manual_registration`` is read-only compatibility for an
    # inventory written by the pre-generic installer. New records use the
    # vendor-neutral ``ready_for_operator`` phase.
    if any(record.get('phase') not in COMPLETE_PHASES for record in records(inventory)):
        raise RuntimeError('resume or quarantine the incomplete device before adding another')
    if any(record['proxy_hash'] == proxy_hash for record in records(inventory)):
        raise RuntimeError('proxy account/session already assigned; use a dedicated sticky session')
    index = inventory['next_index']
    if index > DEVICE_LIMIT:
        raise RuntimeError('device address/port allocator is exhausted')
    device = device_id(index)
    record = {'id': device, 'phone_hash': phone_hash, 'proxy_hash': proxy_hash,
              'request_hash': request_hash, 'phase': 'reserved'}
    inventory['devices'][device] = record
    inventory['next_index'] = index + 1
    return record, True
