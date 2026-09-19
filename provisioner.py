#!/usr/bin/env python3
"""Unified, fail-closed operator CLI for the Android Farm."""
import argparse
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

from ops import inventory, resources
from ops.compose_factory import PROFILES, canonical_device, single_instance
from ops.secureio import read_private_json, require_trusted_release_file, require_trusted_release_tree

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path('/etc/android-farm/provisioner.json')


@dataclass(frozen=True)
class Config:
    compose_file: Path
    compose_project: str
    secret_dir: Path
    backup_dir: Path
    console_url: str
    state_dir: Path = Path('/var/lib/android-farm')
    apk_trust_file: Path | None = None


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
    parsed = urlparse(value['console_url'])
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError('console_url must be a clean HTTPS origin')
    state_dir = Path(value.get('state_dir', '/var/lib/android-farm')).resolve()
    if state_dir != Path('/var/lib/android-farm'):
        raise RuntimeError('state_dir is fixed at /var/lib/android-farm for all host guards')
    trust = Path(value['apk_trust_file']).resolve() if value.get('apk_trust_file') else None
    return Config(compose, project,
                  Path(value.get('secret_dir', '/etc/android-farm/secrets')).resolve(),
                  Path(value.get('backup_dir', '/var/backups/android-farm')).resolve(),
                  value['console_url'].rstrip('/'),
                  state_dir, trust)


def require_host_root():
    if os.name != 'posix' or os.geteuid() != 0:
        raise RuntimeError('run this command as root on the Ubuntu Docker host')
    resources.local_docker()


def invoke(script, *arguments, capture=False):
    module = f'ops.{Path(script).stem}'
    command = [sys.executable, '-m', module, *map(str, arguments)]
    return subprocess.run(command, check=True, text=True, capture_output=capture, timeout=900, cwd=ROOT)


def farmctl(config, action, device, *extra, capture=False):
    return invoke('farmctl.py', action, device, '--compose', config.compose_file,
                  '--project', config.compose_project, '--secret-dir', config.secret_dir,
                  '--backup-dir', config.backup_dir, *extra, capture=capture)


def web_url(config, device):
    return f'{config.console_url}/d/{device}/'


def proxy_endpoint(config, device):
    path = config.secret_dir / f'{device}.json'
    if not path.exists():
        return '-'
    secret = read_private_json(path, f'{device} proxy secret')
    return f'{secret.get("type", "proxy")}://{secret.get("server", "?")}:{secret.get("server_port", "?")}'


def docker_inspect(name):
    result = subprocess.run(['docker', 'inspect', name], text=True, capture_output=True, timeout=20)
    if result.returncode:
        return None
    return json.loads(result.stdout)[0]


def collect_status(config):
    state = inventory.load(config.state_dir / 'inventory.json')
    hold_path = config.state_dir / 'holds.json'
    holds = read_private_json(hold_path, 'safety holds') if hold_path.exists() else {}
    rows = []
    running_names = []
    inspections = {}
    for record in inventory.records(state):
        device = record['id']
        role_states = {}
        for role in ('proxy', 'android', 'screen'):
            name = f'{role}-{device}'
            item = docker_inspect(name)
            inspections[name] = item
            if item and item['State'].get('Running'):
                running_names.append(name)
            role_states[role] = ('missing' if not item else
                                 item['State'].get('Health', {}).get('Status') or
                                 ('running' if item['State'].get('Running') else 'stopped'))
        proxy = inspections[f'proxy-{device}']
        bindings = ((proxy or {}).get('NetworkSettings', {}).get('Ports', {}).get('5555/tcp') or [])
        adb_port = bindings[0].get('HostPort') if bindings else None
        rows.append({'id': device, 'phase': record.get('phase', 'unknown'), 'hold': holds.get(device),
                     'containers': role_states, 'adb': f'127.0.0.1:{adb_port}' if adb_port else None,
                     'screen': web_url(config, device), 'proxy': proxy_endpoint(config, device),
                     'expected_egress_ip': record.get('expected_egress_ip'),
                     'phone': record.get('phone_masked', '-'), 'cpu': '-', 'memory': '-'})
    if running_names:
        stats = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *running_names],
                               text=True, capture_output=True, check=True, timeout=45)
        by_name = {}
        for line in stats.stdout.splitlines():
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


def build_parser():
    parser = argparse.ArgumentParser(prog='device-provisioner', description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest='command', required=True)
    up = sub.add_parser('up', help='provision the next device or start an allocated device')
    choice = up.add_mutually_exclusive_group(required=True)
    choice.add_argument('--id', dest='device')
    choice.add_argument('--request', type=Path)
    up.add_argument('--apk', type=Path, help='optional approved QA APK installed after start')
    up.add_argument('--apk-sha256')
    up.add_argument('--package')
    up.add_argument('--activity')
    up.add_argument('--grant', action='append', default=[],
                    choices=['android.permission.CAMERA', 'android.permission.READ_CONTACTS',
                             'android.permission.RECORD_AUDIO'])
    down = sub.add_parser('down', help='stop while preserving all data')
    down.add_argument('--id', dest='device', required=True)
    check = sub.add_parser('check', help='check proxy, Android boot and ADB')
    check.add_argument('--id', dest='device', required=True)
    check_ip = sub.add_parser('check-ip', help='compare proxy namespace and Android-shell egress')
    check_ip.add_argument('--id', dest='device', required=True)
    check_ip.add_argument('--json', action='store_true')
    status = sub.add_parser('status', help='show inventory, health, resources and ports')
    status.add_argument('--json', action='store_true')
    backup = sub.add_parser('backup', help='create a consistent offline data backup')
    backup.add_argument('--id', dest='device', required=True)
    hold = sub.add_parser('hold', help='persist a safety hold, then stop the device')
    hold.add_argument('--id', dest='device', required=True)
    hold.add_argument('--reason', required=True,
                      choices=['account-restriction', 'ip-change', 'ownership-review', 'maintenance'])
    release = sub.add_parser('release', help='release a hold after human review')
    release.add_argument('--id', dest='device', required=True)
    release.add_argument('--review-completed', action='store_true', required=True)
    resources_command = sub.add_parser('resources', help='print the live host capacity report')
    resources_command.add_argument('--json', action='store_true')
    render = sub.add_parser('render', help='render one honest Redroid QA profile for Coolify review/import')
    render.add_argument('--id', dest='device', required=True)
    render.add_argument('--profile', choices=sorted(PROFILES), default='phone_hd')
    render.add_argument('--data-root', type=Path, default=Path('/opt/farm/data'))
    render.add_argument('--output', type=Path, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    require_host_root()
    if args.command == 'resources':
        report = resources.probe()
        print(json.dumps(report, indent=2) if args.json else
              f"capacity={report['capacity']} cpu={report['cpu_cores']} ram={report['ram_gib']}GiB "
              f"available={report['available_ram_gib']}GiB disk_free={report['disk_free_gib']}GiB")
        return
    if args.command == 'render':
        document = single_instance(args.device, args.profile, args.data_root)
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise RuntimeError('output exists; refusing to overwrite a reviewed Compose file')
        output.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        print(output)
        return
    config = load_config(args.config)
    if args.command == 'up' and args.request:
        if not config.apk_trust_file:
            raise RuntimeError('request provisioning requires configured apk_trust_file')
        result = invoke('provision.py', '--request', args.request, '--compose', config.compose_file,
                        '--project', config.compose_project, '--secret-dir', config.secret_dir,
                        '--apk-trust-file', config.apk_trust_file, capture=True)
        if result.stdout:
            print(result.stdout, end='')
        matches = re.findall(r'^(num\d{2,3}):', result.stdout or '', re.MULTILINE)
        if not matches:
            raise RuntimeError('provision completed without a device identifier')
        print('screen:', web_url(config, matches[-1]))
    elif args.command == 'up':
        device = canonical_device(args.device)
        farmctl(config, 'start', device)
        if any((args.apk, args.apk_sha256, args.package, args.activity, args.grant)):
            if not all((args.apk, args.apk_sha256, args.package)) or not config.apk_trust_file:
                raise RuntimeError('APK install requires --apk, --apk-sha256, --package and configured apk_trust_file')
            from ops import app_installer
            artifact = app_installer.verify(args.apk, args.apk_sha256, args.package, config.apk_trust_file)
            app_installer.install(device, artifact, args.grant, args.activity)
            print(f'{device}: approved QA application installed and launched')
        print('screen:', web_url(config, device))
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
        payload.update(expected=expected, matches=(payload['proxy_namespace'] == expected == payload['android_shell']))
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
    elif args.command == 'backup':
        farmctl(config, 'backup', canonical_device(args.device))
    elif args.command == 'hold':
        invoke('account_policy.py', 'hold', canonical_device(args.device), '--reason', args.reason)
    elif args.command == 'release':
        invoke('account_policy.py', 'release', canonical_device(args.device), '--review-completed')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
