"""Read-only identity drift guard. Does not alter Android properties or identifiers."""
import hashlib
from pathlib import Path
import subprocess
import time

try:
    from .device_ids import device_index, network_plan
except ImportError:
    from device_ids import device_index, network_plan


def snapshot(device, timeout=300):
    target = f"{network_plan(device_index(device, aliases=False))['proxy_control_ip']}:5555"
    base = ['docker', 'exec', f'screen-{device}', 'adb', '-s', target, 'shell']
    def read(*args):
        return subprocess.check_output([*base, *args], text=True, stderr=subprocess.DEVNULL, timeout=15).strip()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if read('getprop', 'sys.boot_completed') == '1':
                break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
    else:
        raise RuntimeError('identity check: Android boot timed out')
    props = {key: read('getprop', key) for key in ('ro.serialno', 'ro.product.model',
             'ro.product.brand', 'ro.product.manufacturer', 'ro.build.fingerprint')}
    if props['ro.serialno'] != f'farm-{device}':
        raise RuntimeError('configured serial changed')
    # This is the shell user's setting, not an app-scoped identifier or a
    # hardware-attestation signal.
    setting = read('settings', 'get', 'secure', 'android_id')
    if not setting or setting == 'null':
        raise RuntimeError('Android identity setting unavailable')
    props['shell_android_id_sha256'] = hashlib.sha256(setting.encode()).hexdigest()
    return props


def verify(device, directory=Path('/var/lib/android-farm/identities')):
    try:
        from . import inventory as device_inventory
        from .secureio import atomic_json, read_private_json, require_private_directory
    except ImportError:
        import inventory as device_inventory
        from secureio import atomic_json, read_private_json, require_private_directory
    current = snapshot(device)
    directory = require_private_directory(directory, 'identity baseline directory', create=True)
    baseline = directory / f'{device}.json'
    inventory_path = directory.parent / 'inventory.json'
    state = device_inventory.load(inventory_path) if inventory_path.exists() else None
    record = device_inventory.find(state, device) if state else None
    if baseline.exists():
        if read_private_json(baseline, 'identity baseline') != current:
            raise RuntimeError('device identity drift detected; stopped for manual review, no automatic reset')
        if record and not record.get('identity_baseline_created'):
            record['identity_baseline_created'] = True
            device_inventory.save(inventory_path, state)
    else:
        if not record or record.get('identity_baseline_created') or record.get('phase') != 'identity_baselining':
            raise RuntimeError('identity baseline missing; restore protected state or use the initial provisioning workflow')
        atomic_json(baseline, current)
        record['identity_baseline_created'] = True
        device_inventory.save(inventory_path, state)
