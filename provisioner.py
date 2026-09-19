#!/usr/bin/env python3
"""Unified, fail-closed operator CLI for the Android Farm."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.parse import urlparse

# Releases are verified against an exact manifest. Root can write bytecode even
# into mode-0555 directories, so protect both this process and its child tools.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

from ops import inventory, resources
from ops.compose_factory import PROFILES, canonical_device, single_instance
from ops.device_profiles import apply_profile, load as load_device_profile
from ops.account_policy import assert_not_held
from ops.identity import verify as verify_identity
from ops.secureio import (atomic_json, read_private_json, require_private_file, require_trusted_release_file,
                          require_trusted_release_tree)

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path('/etc/android-farm/provisioner.json')


@dataclass(frozen=True)
class Config:
    compose_file: Path
    compose_env_file: Path | None
    compose_project: str
    secret_dir: Path
    backup_dir: Path
    console_url: str
    state_dir: Path = Path('/var/lib/android-farm')
    apk_trust_file: Path | None = None
    proxy_registry: Path = Path('/var/lib/android-farm/proxies.json')
    proxy_store_dir: Path = Path('/etc/android-farm/proxies')
    access_mode: str = 'domain'


def validate_console_origin(value, access_mode='domain'):
    """Accept HTTP only for explicit IP mode and otherwise retain HTTPS configs."""
    if (not isinstance(value, str) or not value or
            any(char.isspace() or ord(char) == 127 for char in value) or
            any(marker in value for marker in ('\\', '?', '#'))):
        raise RuntimeError('console_url must be a clean browser origin')
    if access_mode not in {'domain', 'ip'}:
        raise RuntimeError('access_mode must be domain or ip')
    parsed = urlparse(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or
            parsed.fragment or parsed.path not in {'', '/'} or parsed.params):
        raise RuntimeError('console_url must be a clean browser origin')
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError('console_url has an invalid port') from exc
    if access_mode == 'domain':
        if parsed.scheme != 'https':
            raise RuntimeError('domain console_url must use HTTPS')
        return value.rstrip('/')
    if parsed.scheme != 'http' or port is None:
        raise RuntimeError('IP-mode console_url must be an explicit HTTP IPv4 origin')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise RuntimeError('IP-mode console_url must contain a literal IPv4 address') from exc
    if (not isinstance(address, ipaddress.IPv4Address) or address.is_unspecified or
            address.is_loopback or address.is_link_local or address.is_multicast or
            address.is_reserved):
        raise RuntimeError('IP-mode console_url has an unusable IPv4 address')
    if not 1024 <= port <= 65535 or 5551 <= port <= 13742:
        raise RuntimeError('IP-mode console_url port is unavailable or reserved for ADB')
    return f'http://{address}:{port}'


def load_config(path):
    value = read_private_json(path, 'provisioner config')
    required = {'compose_file', 'compose_project', 'console_url'}
    missing = sorted(required - value.keys())
    if missing:
        raise RuntimeError('provisioner config missing: ' + ', '.join(missing))
    compose = require_trusted_release_file(Path(value['compose_file']), 'configured Compose release')
    # The two locally built images are part of the root Docker operation. Keep
    # their build inputs in the same reviewed release tree as the Compose file.
    release_root = compose.parent
    require_trusted_release_tree(release_root / 'images', 'image build inputs')
    dockerignore = release_root / '.dockerignore'
    if dockerignore.exists():
        require_trusted_release_file(dockerignore, '.dockerignore')
    project = value['compose_project']
    if not isinstance(project, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', project):
        raise RuntimeError('invalid Compose project name')
    access_mode = value.get('access_mode', 'domain')
    console_url = validate_console_origin(value['console_url'], access_mode)
    state_dir = Path(value.get('state_dir', '/var/lib/android-farm')).resolve()
    if state_dir != Path('/var/lib/android-farm'):
        raise RuntimeError('state_dir is fixed at /var/lib/android-farm for all host guards')
    trust = Path(value['apk_trust_file']).resolve() if value.get('apk_trust_file') else None
    env_file = require_private_file(Path(value['compose_env_file']).resolve(), 'Compose environment') if value.get('compose_env_file') else None
    return Config(compose_file=compose, compose_env_file=env_file, compose_project=project,
                  secret_dir=Path(value.get('secret_dir', '/etc/android-farm/secrets')).resolve(),
                  backup_dir=Path(value.get('backup_dir', '/var/backups/android-farm')).resolve(),
                  console_url=console_url, state_dir=state_dir,
                  apk_trust_file=trust,
                  proxy_registry=Path(value.get('proxy_registry', '/var/lib/android-farm/proxies.json')).resolve(),
                  proxy_store_dir=Path(value.get('proxy_store_dir', '/etc/android-farm/proxies')).resolve(),
                  access_mode=access_mode)


def require_host_root():
    if os.name != 'posix' or os.geteuid() != 0:
        raise RuntimeError('run this command as root on the Ubuntu Docker host')
    resources.local_docker()


@contextmanager
def host_lifecycle_lock(path=Path('/run/lock/android-farm.lock')):
    """Coordinate credential changes with guarded start/stop/backup operations."""
    import fcntl
    with path.open('w') as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield


def invoke(script, *arguments, capture=False):
    """Run an ops module; its stderr always streams through to this process.

    Only stdout is captured on request. Swallowing stderr hid the deliberate
    fail-closed messages that the console's failure mapping and the host job
    log depend on, leaving operators with a generic failure.
    """
    module = f'ops.{Path(script).stem}'
    command = [sys.executable, '-m', module, *map(str, arguments)]
    return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE if capture else None,
                          timeout=3300, cwd=ROOT)


def describe_failure(exc):
    """One operator-facing line for a failed host step, never a command's full argv."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f'host step timed out after {int(exc.timeout or 0)} seconds; retry when the host is idle'
    if isinstance(exc, subprocess.CalledProcessError):
        argv = [str(part) for part in (exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd])]
        name = argv[2] if len(argv) > 2 and argv[1] == '-m' else (Path(argv[0]).name if argv else 'command')
        return f'host step {name} exited with status {exc.returncode}; its messages are printed above'
    return str(exc)


def farmctl(config, action, device, *extra, capture=False):
    env_args = ('--env-file', config.compose_env_file) if config.compose_env_file else ()
    return invoke('farmctl.py', action, device, '--compose', config.compose_file, *env_args,
                  '--project', config.compose_project, '--access-mode', config.access_mode,
                  '--secret-dir', config.secret_dir,
                  '--proxy-registry', config.proxy_registry,
                  '--proxy-store-dir', config.proxy_store_dir,
                  '--backup-dir', config.backup_dir, *extra, capture=capture)


def web_url(config, device):
    return f'{config.console_url}/d/{device}/'


def proxy_endpoint(config, device):
    path = config.secret_dir / f'{device}.json'
    if not path.exists():
        return '-'
    secret = read_private_json(path, f'{device} proxy secret')
    if secret.get('type') == 'direct':
        return 'direct'
    return f'{secret.get("type", "proxy")}://{secret.get("server", "?")}:{secret.get("server_port", "?")}'


def validate_managed_proxy(config, device, record):
    """Fail closed when a registry-managed proxy is disabled, moved or stale."""
    proxy_id = record.get('proxy_id')
    if record.get('egress') == 'direct':
        installed = read_private_json(config.secret_dir / f'{device}.json',
                                      f'{device} installed egress secret')
        if proxy_id or installed != {'type': 'direct'}:
            raise RuntimeError('direct egress device has an unexpected proxy configuration')
        return
    if not proxy_id:
        return
    from ops.proxy_store import ProxyStore
    store = ProxyStore(config.proxy_registry, config.proxy_store_dir)
    metadata = store.show(proxy_id)
    if metadata.get('state') != 'enabled' or metadata.get('assigned_device') != device:
        raise RuntimeError('managed proxy is disabled or no longer assigned to this device')
    if metadata.get('expected_egress_ip') != record.get('expected_egress_ip'):
        raise RuntimeError('managed proxy expected IP differs from the persistent allocation')
    installed = read_private_json(config.secret_dir / f'{device}.json',
                                  f'{device} installed proxy secret')
    current = store.provisioning_secret(proxy_id)
    keys = ('type', 'server', 'server_port', 'username', 'password')
    if any(installed.get(key) != current.get(key) for key in keys):
        raise RuntimeError('installed proxy credential differs from the managed registry')


def docker_inspect(name):
    result = subprocess.run(['docker', 'inspect', name], text=True, capture_output=True, timeout=20)
    if result.returncode:
        return None
    return json.loads(result.stdout)[0]


def bulk_managed_inspections(devices, runner=subprocess.run, chunk_size=128):
    """Inspect only materialized fleet containers with bounded Docker calls."""
    expected = {f'{role}-{device}' for device in devices for role in ('proxy', 'android', 'screen')}
    listing = runner(['docker', 'ps', '-a', '--filter', 'label=farm.stack=devices',
                      '--format', '{{.Names}}'], text=True, capture_output=True, timeout=30)
    if listing.returncode:
        raise RuntimeError('failed to list managed device containers')
    names = sorted(expected.intersection(line.strip() for line in listing.stdout.splitlines()))
    found = {}
    for offset in range(0, len(names), chunk_size):
        result = runner(['docker', 'inspect', *names[offset:offset + chunk_size]],
                        text=True, capture_output=True, timeout=60)
        # Docker can return non-zero if a container disappears after ``ps``;
        # any valid objects still returned are safe to use and the rest remain
        # "missing" in this point-in-time status snapshot.
        if not result.stdout.strip():
            continue
        try:
            items = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError('Docker returned invalid inspection data') from exc
        for item in items if isinstance(items, list) else []:
            name = str(item.get('Name', '')).lstrip('/')
            if name in expected:
                found[name] = item
    return found


def all_roles_running(device):
    for role in ('proxy', 'android', 'screen'):
        item = docker_inspect(f'{role}-{device}')
        if not item or not item.get('State', {}).get('Running'):
            return False
    return True


def accept_idempotent_running(config, device, record):
    """Prove an already-running device before treating a replayed up task as success."""
    if not all_roles_running(device):
        return False
    assert_not_held(device, config.state_dir / 'holds.json')
    farmctl(config, 'check', device)
    verify_identity(device)
    result = farmctl(config, 'ip', device, capture=True)
    payload = json.loads(result.stdout)
    expected = record.get('expected_egress_ip')
    if expected is None and record.get('egress') == 'direct':
        # Unpinned direct egress: the namespace and the Android shell must agree.
        expected = payload.get('proxy_namespace')
    if not expected or payload.get('proxy_namespace') != expected or payload.get('android_shell') != expected:
        try:
            invoke('account_policy.py', 'hold', device, '--reason', 'ip-change')
        except subprocess.SubprocessError as exc:
            raise RuntimeError('egress mismatch and automatic safety hold failed') from exc
        raise RuntimeError('egress mismatch; device was placed on a safety hold and stopped')
    return True


def collect_status(config):
    state = inventory.load(config.state_dir / 'inventory.json')
    hold_path = config.state_dir / 'holds.json'
    holds = read_private_json(hold_path, 'safety holds') if hold_path.exists() else {}
    rows = []
    running_names = []
    records = inventory.records(state)
    inspections = bulk_managed_inspections([record['id'] for record in records])
    for record in records:
        device = record['id']
        role_states = {}
        for role in ('proxy', 'android', 'screen'):
            name = f'{role}-{device}'
            item = inspections.get(name)
            if item and item['State'].get('Running'):
                running_names.append(name)
            role_states[role] = ('missing' if not item else
                                 item['State'].get('Health', {}).get('Status') or
                                 ('running' if item['State'].get('Running') else 'stopped'))
        proxy = inspections.get(f'proxy-{device}')
        bindings = ((proxy or {}).get('NetworkSettings', {}).get('Ports', {}).get('5555/tcp') or [])
        adb_port = bindings[0].get('HostPort') if bindings else None
        rows.append({'id': device, 'phase': record.get('phase', 'unknown'), 'hold': holds.get(device),
                     'containers': role_states, 'adb': f'127.0.0.1:{adb_port}' if adb_port else None,
                     'screen': web_url(config, device), 'proxy': proxy_endpoint(config, device),
                     'expected_egress_ip': record.get('expected_egress_ip'),
                     'phone': record.get('phone_masked', '-'), 'cpu': '-', 'memory': '-',
                     'last_error': record.get('last_error'), 'failed_at': record.get('failed_at')})
    if running_names:
        # A container that stops between ps and stats must not fail the whole report.
        stats = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *running_names],
                               text=True, capture_output=True, check=False, timeout=45)
        by_name = {}
        for line in (stats.stdout or '').splitlines():
            if line.strip():
                item = json.loads(line)
                by_name[item['Name']] = item
        for row in rows:
            item = by_name.get(f'android-{row["id"]}')
            if item:
                row['cpu'], row['memory'] = item.get('CPUPerc', '-'), item.get('MemUsage', '-')
    report = resources.probe()
    return {'schema_version': 1, 'collected_at': int(time.time()), 'capacity': report['capacity'],
            'active': sum(1 for row in rows if row['containers']['android'] == 'running'), 'devices': rows}


def print_status(payload):
    print(f"capacity={payload['capacity']} active={payload['active']} devices={len(payload['devices'])}")
    headers = ('ID', 'PHASE', 'ANDROID', 'CPU', 'MEMORY', 'ADB', 'PROXY', 'HOLD')
    values = []
    for row in payload['devices']:
        values.append((row['id'], row['phase'], row['containers']['android'], row['cpu'], row['memory'],
                       row['adb'] or '-', row['proxy'], (row['hold'] or {}).get('reason', '-')))
    widths = [max(len(headers[index]), *(len(str(row[index])) for row in values)) if values else len(headers[index])
              for index in range(len(headers))]
    print('  '.join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    for row in values:
        print('  '.join(str(value).ljust(widths[index]) for index, value in enumerate(row)))
    for row in payload['devices']:
        if row.get('last_error'):
            print(f"{row['id']}: incomplete preparation - {row['last_error']}")


def _env_value(path, key, default):
    if not path or not Path(path).exists():
        return default
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.startswith(f'{key}='):
            return line.split('=', 1)[1].strip().strip('"').strip("'") or default
    return default


DIAGNOSTIC_LOG_SCAN_LIMIT = 40
DIAGNOSTIC_LOG_READ_BYTES = 64 * 1024


def _diagnostic_log_tail(path):
    """Read a bounded tail from a regular file without following a symlink."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            current = os.fstat(stream.fileno())
            if (not stat.S_ISREG(current.st_mode) or
                    (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                return None
            offset = max(0, current.st_size - DIAGNOSTIC_LOG_READ_BYTES)
            stream.seek(offset)
            data = stream.read(DIAGNOSTIC_LOG_READ_BYTES)
            if offset:
                # A partial first line must not become a fabricated failure marker.
                data = data.partition(b'\n')[2]
        return data.decode('utf-8', errors='replace').splitlines()
    except OSError:
        return None


def diagnostic_job_logs(log_dir, device, *, tail_lines=25):
    """Keep recent attempts plus the last proven preparation failure, at most four logs.

    An unscoped request filename alone does not prove which device it concerns.
    Only its exact device-prefixed failure marker supplies that association.
    """
    directory = Path(log_dir)
    if directory.is_symlink() or not directory.is_dir():
        return []
    pattern = re.compile(r'-(?:up|provision|restart|check|check-ip|remove)'
                         r'(?:-(?P<device>num(?:0[1-9]|[1-9]\d+)))?(?:-\d+)?\.log\Z')
    failure = re.compile(rf'^{re.escape(device)}: preparation stopped during \S')
    other_failure = re.compile(r'^num(?:0[1-9]|[1-9]\d+): preparation stopped during \S')
    try:
        candidates = []
        for path in directory.glob('*.log'):
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    candidates.append(path)
            except OSError:
                continue
        candidates = sorted(candidates, key=lambda path: path.name)[-DIAGNOSTIC_LOG_SCAN_LIMIT:]
    except OSError:
        return []
    relevant = []
    origin = None
    for path in candidates:
        match = pattern.search(path.name)
        if not match or match['device'] not in (None, device):
            continue
        lines = _diagnostic_log_tail(path)
        if lines is None:
            continue
        actual_failure = any(failure.match(line) for line in lines)
        if match['device'] is None and not actual_failure and any(other_failure.match(line) for line in lines):
            continue
        entry = {'name': path.name, 'tail': lines[-tail_lines:],
                 'association': 'device' if match['device'] == device or actual_failure else 'unscoped'}
        relevant.append(entry)
        if actual_failure:
            origin = entry
    selected = relevant[-3:]
    if origin is not None and origin not in selected:
        selected.insert(0, origin)
    return selected


def collect_diagnosis(config, device, *, log_dir=Path('/var/lib/android-farm/job-logs'), log_tail=60,
                      container_tail=40):
    """Read-only, bounded evidence for one device: record, containers, host prerequisites, logs.

    Everything here already lives on the host for root; nothing is sent anywhere.
    """
    from ops import desired_state, events
    import platform
    state = inventory.load(config.state_dir / 'inventory.json')
    record = inventory.find(state, device) or {}
    hold_path = config.state_dir / 'holds.json'
    holds = read_private_json(hold_path, 'safety holds') if hold_path.exists() else {}
    try:
        wants_running = desired_state.wants_running(device)
    except (OSError, RuntimeError, ValueError):
        wants_running = None
    report = {'schema_version': 1, 'device': device, 'collected_at': int(time.time()), 'allocated': bool(record),
              'record': {key: record.get(key) for key in (
                  'phase', 'egress', 'proxy_id', 'last_error', 'failed_at', 'identity_baseline_created',
                  'apk_package', 'prepared_at', 'expected_egress_ip')},
              'hold': holds.get(device), 'wants_running': wants_running, 'containers': {}}
    for role in ('proxy', 'android', 'screen'):
        name = f'{role}-{device}'
        item = docker_inspect(name)
        if not item:
            report['containers'][role] = {'exists': False}
            continue
        status = item.get('State') or {}
        entry = {'exists': True, 'running': bool(status.get('Running')), 'paused': bool(status.get('Paused')),
                 'exit_code': status.get('ExitCode'), 'oom_killed': bool(status.get('OOMKilled')),
                 'health': (status.get('Health') or {}).get('Status'), 'started_at': status.get('StartedAt'),
                 'finished_at': status.get('FinishedAt'), 'error': status.get('Error') or None,
                 'image': (item.get('Config') or {}).get('Image')}
        logs = subprocess.run(['docker', 'logs', '--tail', str(container_tail), name],
                              text=True, capture_output=True, timeout=30)
        entry['log_tail'] = ((logs.stdout or '') + (logs.stderr or '')).splitlines()[-container_tail:]
        report['containers'][role] = entry
    override = config.state_dir / 'device-overrides' / f'{device}.json'
    image = _env_value(config.compose_env_file, 'REDROID_IMAGE', 'redroid/redroid:12.0.0-latest')
    if override.exists():
        try:
            image = read_private_json(override, 'device override')['services'][f'android-{device}'].get('image', image)
        except (RuntimeError, KeyError, TypeError):
            pass
    filesystems = Path('/proc/filesystems')
    meminfo = {}
    if Path('/proc/meminfo').exists():
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, value = line.partition(':')
            meminfo[key] = int(value.split()[0]) * 1024 if value.split() else 0
    report['host'] = {
        'kernel': platform.release(),
        'binderfs': 'binder' in (filesystems.read_text() if filesystems.exists() else ''),
        'binder_nodes': [node for node in ('/dev/binder', '/dev/binderfs') if Path(node).exists()],
        'android_image': image,
        'android_image_present': subprocess.run(['docker', 'image', 'inspect', image],
                                                capture_output=True, timeout=20).returncode == 0,
        'load_1m': round(os.getloadavg()[0], 2) if hasattr(os, 'getloadavg') else None,
        'available_ram_gib': round(meminfo.get('MemAvailable', 0) / 1024 ** 3, 2),
    }
    report['files'] = {
        'secret': (config.secret_dir / f'{device}.json').exists(),
        'override': override.exists(),
        'identity_baseline': (config.state_dir / 'identities' / f'{device}.json').exists(),
        'profile': (Path('/etc/android-farm/device-profiles') / f'{device}.json').exists(),
        'data_directory': (Path('/opt/farm/data/instances') / device).exists(),
        'volume': subprocess.run(['docker', 'volume', 'inspect', f'redroid-data-{device}'],
                                 capture_output=True, timeout=20).returncode == 0,
    }
    try:
        report['events'] = events.recent(15, device=device)
    except (OSError, RuntimeError, ValueError):
        report['events'] = []
    report['job_logs'] = diagnostic_job_logs(log_dir, device, tail_lines=log_tail)
    return report


def print_diagnosis(report):
    record = report['record']
    print(f"device={report['device']} allocated={report['allocated']} phase={record.get('phase')} "
          f"egress={record.get('egress') or 'proxy'} hold={(report.get('hold') or {}).get('reason') or '-'} "
          f"wants_running={report.get('wants_running')}")
    if record.get('last_error'):
        print(f"last_error: {record['last_error']}")
    host = report['host']
    print(f"host: kernel={host['kernel']} binderfs={host['binderfs']} binder_nodes={host['binder_nodes']} "
          f"image={host['android_image']} image_present={host['android_image_present']} "
          f"load_1m={host['load_1m']} available_ram_gib={host['available_ram_gib']}")
    print('files: ' + ' '.join(f'{key}={value}' for key, value in report['files'].items()))
    for role, entry in report['containers'].items():
        if not entry.get('exists'):
            print(f'{role}: missing')
            continue
        print(f"{role}: running={entry['running']} paused={entry['paused']} exit_code={entry['exit_code']} "
              f"oom_killed={entry['oom_killed']} health={entry['health']} image={entry['image']} "
              f"started={entry['started_at']} finished={entry['finished_at']} error={entry['error']}")
        for line in entry.get('log_tail', []):
            print(f'  | {line}')
    print('events:')
    for event in report['events']:
        print(f"  {event['at']} {event['kind']} {event.get('detail') or ''}")
    for log in report['job_logs']:
        scope = ' [unscoped; device association not established]' if log.get('association') == 'unscoped' else ''
        print(f"job log {log['name']}{scope}:")
        for line in log['tail']:
            print(f'  | {line}')


def build_parser():
    parser = argparse.ArgumentParser(prog='device-provisioner', description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest='command', required=True)
    up = sub.add_parser('up', help='provision the next device or start an allocated device')
    choice = up.add_mutually_exclusive_group(required=True)
    choice.add_argument('--id', dest='device')
    choice.add_argument('--request', type=Path)
    up.add_argument('--resume-id', type=canonical_device,
                    help='with --request, continue only this existing device using its original settings')
    up.add_argument('--apk', type=Path, help='optional approved QA APK installed after start')
    up.add_argument('--apk-sha256')
    up.add_argument('--package')
    up.add_argument('--activity')
    up.add_argument('--grant', action='append', default=[],
                    choices=['android.permission.CAMERA', 'android.permission.READ_CONTACTS',
                             'android.permission.RECORD_AUDIO'])
    down = sub.add_parser('down', help='stop while preserving all data')
    down.add_argument('--id', dest='device', required=True)
    restart = sub.add_parser('restart', help='guarded stop followed by the attested start of an allocated device')
    restart.add_argument('--id', dest='device', required=True)
    check = sub.add_parser('check', help='check proxy, Android boot and ADB')
    check.add_argument('--id', dest='device', required=True)
    check_ip = sub.add_parser('check-ip', help='compare proxy namespace and Android-shell egress')
    check_ip.add_argument('--id', dest='device', required=True)
    check_ip.add_argument('--json', action='store_true')
    status = sub.add_parser('status', help='show inventory, health, resources and ports')
    status.add_argument('--json', action='store_true')
    diagnose = sub.add_parser('diagnose', help='read-only evidence for one device: record, containers, host, logs')
    diagnose.add_argument('--id', dest='device', required=True)
    diagnose.add_argument('--json', action='store_true')
    backup = sub.add_parser('backup', help='create a consistent offline data backup')
    backup.add_argument('--id', dest='device', required=True)
    remove = sub.add_parser('remove', help='decommission a device: stop it, release its proxy and drop it from the inventory')
    remove.add_argument('--id', dest='device', required=True)
    remove.add_argument('--purge-data', action='store_true',
                        help='also delete the persistent data, volume, identity baseline and profile (irreversible)')
    hold = sub.add_parser('hold', help='persist a safety hold, then stop the device')
    hold.add_argument('--id', dest='device', required=True)
    hold.add_argument('--reason', required=True,
                      choices=['account-restriction', 'ip-change', 'ownership-review', 'maintenance'])
    release = sub.add_parser('release', help='release a hold after human review')
    release.add_argument('--id', dest='device', required=True)
    release.add_argument('--review-completed', action='store_true', required=True)
    resources_command = sub.add_parser('resources', help='print the live host capacity report')
    resources_command.add_argument('--json', action='store_true')
    render = sub.add_parser('render', help='render one transparent Redroid QA profile for offline review')
    render.add_argument('--id', dest='device', required=True)
    render_profile = render.add_mutually_exclusive_group()
    render_profile.add_argument('--profile', choices=sorted(PROFILES), default='phone_hd',
                                help='built-in transparent QA display profile')
    render_profile.add_argument('--profile-file', type=Path,
                                help='validated JSON profile with Android version, display and locale')
    render.add_argument('--output', type=Path, required=True)
    apps = sub.add_parser('apps', help='manage the private APK repository used by every installation')
    apps_actions = apps.add_subparsers(dest='apps_action', required=True)
    apps_import = apps_actions.add_parser('import', help='copy, verify and register an APK for reuse')
    apps_import.add_argument('--apk', required=True, type=Path, help='root-owned APK, e.g. /root/farm-input/WhatsApp.apk')
    apps_import.add_argument('--id', help='catalog identifier; derived from the package name when omitted')
    apps_import.add_argument('--label', help='name shown in the console')
    apps_import.add_argument('--activity', help='launcher component; read from the APK when omitted')
    apps_import.add_argument('--grant', action='append', default=[],
                             choices=['android.permission.CAMERA', 'android.permission.READ_CONTACTS',
                                      'android.permission.RECORD_AUDIO'])
    apps_import.add_argument('--allow-signer-change', action='store_true',
                             help='accept a new signing certificate after verifying the publisher fingerprint')
    apps_actions.add_parser('list', help='show registered applications without file contents')
    apps_remove = apps_actions.add_parser('remove', help='unregister an application and prune its file')
    apps_remove.add_argument('--id', required=True)
    proxy = sub.add_parser('proxy', help='manage authenticated sticky proxy connections')
    proxy_actions = proxy.add_subparsers(dest='proxy_action', required=True)
    proxy_add = proxy_actions.add_parser('add', help='register a dedicated upstream proxy')
    proxy_add.add_argument('--id', required=True)
    proxy_add.add_argument('--label', required=True)
    proxy_add.add_argument('--type', required=True, choices=['http', 'socks', 'socks5'])
    proxy_add.add_argument('--server', required=True, help='pinned public IPv4')
    proxy_add.add_argument('--port', required=True, type=int)
    proxy_add.add_argument('--username', required=True)
    proxy_add.add_argument('--password-file', required=True, type=Path)
    proxy_add.add_argument('--expected-ip', required=True)
    for action in ('show', 'test', 'disable', 'enable', 'delete'):
        command = proxy_actions.add_parser(action)
        command.add_argument('--id', required=True)
    proxy_list = proxy_actions.add_parser('list')
    proxy_list.add_argument('--enabled-only', action='store_true')
    proxy_rotate = proxy_actions.add_parser('rotate-password')
    proxy_rotate.add_argument('--id', required=True)
    proxy_rotate.add_argument('--password-file', required=True, type=Path)
    proxy_assign = proxy_actions.add_parser('assign')
    proxy_assign.add_argument('--id', required=True)
    proxy_assign.add_argument('--device', required=True)
    proxy_unassign = proxy_actions.add_parser('unassign')
    proxy_unassign.add_argument('--id', required=True)
    proxy_unassign.add_argument('--device')
    return parser


def private_password(path):
    value = require_private_file(path, 'proxy password file').read_text(encoding='utf-8')
    if value.endswith('\n'):
        value = value[:-1]
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RuntimeError('password file must contain one non-empty line without control characters')
    return value


def rotate_managed_proxy_password(store, proxy_id, password_file, config):
    """Rotate both credential copies while starts are excluded by the host lock."""
    from ops import farmctl as farm_operations
    with host_lifecycle_lock():
        record = store.show(proxy_id)
        assigned = record.get('assigned_device')
        old_secret = store.provisioning_secret(proxy_id)
        installed_path = config.secret_dir / f'{assigned}.json' if assigned else None
        if assigned:
            assigned = canonical_device(assigned)
            farm_operations.stop(assigned)
            installed = read_private_json(installed_path, f'{assigned} installed proxy secret')
            old_identity = [old_secret.get(key) for key in ('type', 'server', 'server_port', 'username')]
            installed_identity = [installed.get(key) for key in ('type', 'server', 'server_port', 'username')]
            if installed_identity != old_identity:
                raise RuntimeError('installed proxy secret differs from the registry; repair manually')
        try:
            result = store.rotate_password(proxy_id, private_password(password_file))
            if installed_path:
                atomic_json(installed_path, store.provisioning_secret(proxy_id))
            health = store.check_health(proxy_id)
        except Exception:
            # Keep both copies on the last known credential when the new
            # password cannot prove the pinned egress IP.
            try:
                store.rotate_password(proxy_id, old_secret['password'])
                if installed_path:
                    atomic_json(installed_path, old_secret)
            except Exception as rollback_error:
                raise RuntimeError('proxy password rotation failed and rollback needs manual repair') from rollback_error
            raise
        result['health'] = health
        result['assigned_device_stopped'] = bool(assigned)
        return result


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'up' and args.resume_id is not None and args.request is None:
        parser.error('--resume-id requires --request; it cannot be used with --id')
    require_host_root()
    if args.command == 'resources':
        report = resources.probe()
        print(json.dumps(report, indent=2) if args.json else
              f"capacity={report['capacity']} cpu={report['cpu_cores']} ram={report['ram_gib']}GiB "
              f"available={report['available_ram_gib']}GiB disk_free={report['disk_free_gib']}GiB")
        return
    if args.command == 'render':
        document = single_instance(args.device, args.profile)
        if args.profile_file:
            document = apply_profile(document, canonical_device(args.device),
                                     load_device_profile(args.profile_file))
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise RuntimeError('output exists; refusing to overwrite a reviewed Compose file')
        output.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        print(output)
        return
    config = load_config(args.config)
    if args.command == 'apps':
        from ops import apk_repository
        catalog = Path('/etc/android-farm/apps.json')
        if not config.apk_trust_file:
            raise RuntimeError('the APK repository requires a configured apk_trust_file')
        with host_lifecycle_lock():
            if args.apps_action == 'import':
                result = apk_repository.import_apk(
                    args.apk, catalog_path=catalog, trust_path=config.apk_trust_file,
                    label=args.label, app_id=args.id, permissions=args.grant, activity=args.activity,
                    allow_signer_change=args.allow_signer_change)
            elif args.apps_action == 'list':
                result = apk_repository.list_apps(catalog)
            else:
                result = apk_repository.remove_app(args.id, catalog_path=catalog)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if args.command == 'proxy':
        from ops.proxy_store import ProxyStore
        store = ProxyStore(config.proxy_registry, config.proxy_store_dir)
        if args.proxy_action == 'add':
            result = store.add(args.id, label=args.label, proxy_type=args.type, server=args.server,
                               server_port=args.port, username=args.username,
                               password=private_password(args.password_file),
                               expected_egress_ip=args.expected_ip)
        elif args.proxy_action == 'list':
            result = store.list(include_disabled=not args.enabled_only)
        elif args.proxy_action == 'show':
            result = store.show(args.id)
        elif args.proxy_action == 'test':
            result = store.check_health(args.id)
        elif args.proxy_action == 'disable':
            assigned = store.show(args.id).get('assigned_device')
            if assigned:
                invoke('account_policy.py', 'hold', canonical_device(assigned),
                       '--reason', 'maintenance')
            result = store.disable(args.id)
            result['assigned_device_stopped'] = bool(assigned)
            result['safety_hold_created'] = bool(assigned)
        elif args.proxy_action == 'enable':
            result = store.enable(args.id)
            try:
                result['health'] = store.check_health(args.id)
            except Exception:
                store.disable(args.id)
                raise RuntimeError('proxy enable failed health validation; proxy remains disabled')
        elif args.proxy_action == 'delete':
            state = inventory.load(config.state_dir / 'inventory.json')
            if any(record.get('proxy_id') == args.id for record in inventory.records(state)):
                raise RuntimeError('proxy belongs to a persistent device and cannot be deleted')
            store.delete(args.id)
            result = {'id': args.id, 'deleted': True}
        elif args.proxy_action == 'rotate-password':
            result = rotate_managed_proxy_password(store, args.id, args.password_file, config)
        elif args.proxy_action == 'assign':
            result = store.assign(args.id, canonical_device(args.device))
        else:
            assigned = (canonical_device(args.device) if args.device
                        else store.show(args.id).get('assigned_device'))
            state = inventory.load(config.state_dir / 'inventory.json')
            if assigned and inventory.find(state, assigned):
                raise RuntimeError('persistent device proxy assignments cannot be removed; use a reviewed device migration')
            result = store.unassign(args.id, assigned)
        print(json.dumps(result, indent=2))
    elif args.command == 'up' and args.request:
        if not config.apk_trust_file:
            raise RuntimeError('request provisioning requires configured apk_trust_file')
        result = invoke('provision.py', '--request', args.request, '--compose', config.compose_file,
                        *(('--resume-id', args.resume_id) if args.resume_id is not None else ()),
                        *(( '--env-file', config.compose_env_file) if config.compose_env_file else ()),
                        '--project', config.compose_project, '--access-mode', config.access_mode,
                        '--secret-dir', config.secret_dir,
                        '--proxy-registry', config.proxy_registry,
                        '--proxy-store-dir', config.proxy_store_dir,
                        '--apk-trust-file', config.apk_trust_file, capture=True)
        if result.stdout:
            print(result.stdout, end='')
        matches = re.findall(r'^(num\d{2,}):', result.stdout or '', re.MULTILINE)
        if not matches:
            raise RuntimeError('provision completed without a device identifier')
        print('screen:', web_url(config, matches[-1]))
    elif args.command in ('up', 'restart'):
        device = canonical_device(args.device)
        record = inventory.find(inventory.load(config.state_dir / 'inventory.json'), device)
        if not record:
            raise RuntimeError('device is not allocated in the managed inventory')
        validate_managed_proxy(config, device, record)
        if args.command == 'restart':
            # A restart is a full guarded stop followed by the same attested
            # start path; it is never a bare `docker restart`.
            farmctl(config, 'stop', device)
            already_running = False
        else:
            already_running = accept_idempotent_running(config, device, record)
        if not already_running:
            farmctl(config, 'start', device)
        if args.command == 'up' and any((args.apk, args.apk_sha256, args.package, args.activity, args.grant)):
            if not all((args.apk, args.apk_sha256, args.package)) or not config.apk_trust_file:
                raise RuntimeError('APK install requires --apk, --apk-sha256, --package and configured apk_trust_file')
            from ops import app_installer
            artifact = app_installer.verify(args.apk, args.apk_sha256, args.package, config.apk_trust_file)
            app_installer.install(device, artifact, args.grant, args.activity)
            print(f'{device}: approved QA application installed and launched')
        print(('already running; screen:' if already_running else 'screen:'), web_url(config, device))
    elif args.command == 'down':
        farmctl(config, 'stop', canonical_device(args.device))
    elif args.command == 'check':
        farmctl(config, 'check', canonical_device(args.device))
    elif args.command == 'check-ip':
        device = canonical_device(args.device)
        result = farmctl(config, 'ip', device, capture=True)
        payload = json.loads(result.stdout)
        record = inventory.find(inventory.load(config.state_dir / 'inventory.json'), device)
        if not record:
            raise RuntimeError('device is not allocated in the managed inventory')
        expected = record.get('expected_egress_ip')
        if expected is None and record.get('egress') == 'direct':
            expected = payload['proxy_namespace']
        payload.update(expected=expected, egress=record.get('egress', 'proxy'),
                       matches=bool(expected) and (payload['proxy_namespace'] == expected == payload['android_shell']))
        print(json.dumps(payload, indent=2) if args.json else
              f"{device}: proxy={payload['proxy_namespace']} android={payload['android_shell']} expected={expected} "
              f"match={'yes' if payload['matches'] else 'NO'}")
        if not payload['matches']:
            try:
                invoke('account_policy.py', 'hold', device, '--reason', 'ip-change')
            except subprocess.SubprocessError as exc:
                raise RuntimeError('egress mismatch and automatic safety hold failed; cut host egress immediately') from exc
            raise RuntimeError('egress mismatch; device was placed on a safety hold and stopped')
    elif args.command == 'status':
        payload = collect_status(config)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print_status(payload)
    elif args.command == 'diagnose':
        report = collect_diagnosis(config, canonical_device(args.device))
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_diagnosis(report)
    elif args.command == 'backup':
        farmctl(config, 'backup', canonical_device(args.device))
    elif args.command == 'remove':
        farmctl(config, 'remove', canonical_device(args.device), *(('--purge-data',) if args.purge_data else ()))
    elif args.command == 'hold':
        invoke('account_policy.py', 'hold', canonical_device(args.device), '--reason', args.reason)
    elif args.command == 'release':
        invoke('account_policy.py', 'release', canonical_device(args.device), '--review-completed')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(describe_failure(exc), file=sys.stderr)
        sys.exit(1)
