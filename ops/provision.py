#!/usr/bin/env python3
"""Provision exactly one sequential device using a private request file."""
import argparse
import contextlib
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

try:
    from . import app_installer, farmctl, inventory, resources
    from .secureio import atomic_json, read_private_json, require_private_directory
except ImportError:
    import app_installer, farmctl, inventory, resources
    from secureio import atomic_json, read_private_json, require_private_directory

ROOT = Path(__file__).resolve().parents[1]


def private_json(path):
    """Backward-compatible import used by older modules."""
    return read_private_json(path, 'request/secret')


def choose_record(records, phone_hash, proxy_hash, request_hash):
    """Compatibility adapter for callers of the original list API."""
    same = next((r for r in records if r['phone_hash'] == phone_hash), None)
    if same:
        if same['request_hash'] != request_hash:
            raise RuntimeError('existing phone has a different request; do not reassign its identity')
        return same
    if any(r['phase'] not in inventory.COMPLETE_PHASES for r in records):
        raise RuntimeError('resume the incomplete device before adding another')
    if any(r['proxy_hash'] == proxy_hash for r in records):
        raise RuntimeError('proxy account/session already assigned; use a dedicated sticky session')
    index = len(records) + 1
    if index > 200:
        raise RuntimeError('maximum device catalog size is 200')
    return dict(id=f'num{index:02d}', phone_hash=phone_hash, proxy_hash=proxy_hash,
                request_hash=request_hash, phase='reserved', created_at=int(time.time()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--compose', required=True, type=Path)
    parser.add_argument('--project', required=True, help='actual Coolify Compose project name')
    parser.add_argument('--secret-dir', type=Path, default=Path('/etc/android-farm/secrets'))
    parser.add_argument('--apk-trust-file', required=True, type=Path,
                        help='root-owned package/signer allowlist, separate from the request')
    args = parser.parse_args()
    args.state_dir = Path('/var/lib/android-farm')
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('run as root on the Ubuntu Docker host')
    resources.local_docker()
    request = read_private_json(args.request, 'device request')
    phone = request['phone']
    if not re.fullmatch(r'\+[1-9]\d{9,14}', phone) or request.get('owner_authorized') is not True:
        raise RuntimeError('valid E.164 phone and owner_authorized=true required')
    expected_ip = str(ipaddress.IPv4Address(request['expected_egress_ip']))
    if not ipaddress.ip_address(expected_ip).is_global:
        raise RuntimeError('expected egress IP must be public')
    secret = read_private_json(request['proxy_file'], 'proxy secret')
    spec = importlib.util.spec_from_file_location('proxy_config', ROOT / 'images/proxy/configure.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.make_config(secret)
    if not secret.get('username') or not secret.get('password'):
        raise RuntimeError('a dedicated authenticated sticky proxy session is required')
    apk_path = Path(request['apk_path']).resolve(strict=True)
    if apk_path.stat().st_uid != 0 or apk_path.stat().st_mode & 0o022:
        raise RuntimeError('APK must be root-owned and not writable by group/others')
    for parent in apk_path.parents:
        if parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise RuntimeError('APK must reside under protected root-owned directories, e.g. /root/farm-input')
    package = request.get('apk_package')
    permissions = request.get('apk_permissions', [])
    activity = request.get('apk_activity')
    if not isinstance(permissions, list) or not all(isinstance(value, str) for value in permissions):
        raise RuntimeError('apk_permissions must be a list of explicit Android permission names')
    if activity is not None and not isinstance(activity, str):
        raise RuntimeError('apk_activity must be a string when provided')
    artifact = app_installer.verify(apk_path, request['apk_sha256'], package, args.apk_trust_file)
    for folder in (args.state_dir, args.secret_dir):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink() or folder.stat().st_uid != 0 or folder.stat().st_mode & 0o077:
            raise RuntimeError('state and secrets directories must be root-owned, chmod 700')
    import fcntl
    with open('/run/lock/android-farm-provision.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        key_path = args.state_dir / 'identity.key'
        registry = args.state_dir / 'inventory.json'
        if not key_path.exists():
            if registry.exists():
                raise RuntimeError('identity key missing: restore it with inventory backup')
            with key_path.open('xb') as stream:
                os.chmod(key_path, 0o600)
                stream.write(secrets.token_bytes(32))
        if (key_path.is_symlink() or key_path.stat().st_mode & 0o077 or
                key_path.stat().st_uid != 0 or key_path.stat().st_size != 32):
            raise RuntimeError('identity key permissions invalid')
        key = key_path.read_bytes()
        digest = lambda value: hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
        inventory_state = inventory.load(registry)
        proxy_identity = json.dumps([secret['type'], secret['server'], secret['server_port'], secret['username']])
        # Credential rotation must not orphan a persistent device. Bind the
        # allocation to the approved endpoint/session identity and immutable
        # QA artifact metadata, never to the password or local file paths.
        immutable_request = {'phone': phone, 'expected_egress_ip': expected_ip,
                             'apk_sha256': artifact['sha256'], 'apk_package': artifact['package'],
                             'apk_permissions': permissions, 'apk_activity': activity}
        request_hash = digest(json.dumps(immutable_request, sort_keys=True) + proxy_identity)
        record, created = inventory.choose(inventory_state, digest(phone), digest(proxy_identity), request_hash)
        device = record['id']
        if record['phase'] in inventory.COMPLETE_PHASES:
            print(f'{device}: already prepared; no duplicate installation or start')
            return
        compose = ['docker', 'compose', '-p', args.project, '-f', str(args.compose.resolve()),
                   '--profile', 'manual']
        config = json.loads(farmctl.run(*compose, 'config', '--format', 'json', capture=True))
        if f'android-{device}' not in config['services']:
            raise RuntimeError('device absent from Coolify Compose catalog; regenerate and redeploy first')
        # Verify the secret path that the actual deployed Compose will mount.
        wanted_secret = config['services'][f'proxy-{device}']['secrets'][0]['source']
        configured_path = Path(config['secrets'][wanted_secret]['file']).resolve()
        if configured_path != (args.secret_dir / f'{device}.json').resolve():
            raise RuntimeError('Compose secret path differs from --secret-dir; correct Coolify ENV first')
        resources.admission(resources.probe(), len(farmctl.active_devices()))
        if created:
            record.update(phone_masked=phone[:3] + '***' + phone[-4:], created_at=int(time.time()),
                          expected_egress_ip=expected_ip)
            inventory.save(registry, inventory_state)
        elif record.get('expected_egress_ip') != expected_ip:
            raise RuntimeError('approved sticky egress IP differs from the existing allocation')
        volume = f'redroid-data-{device}'
        data_root = require_private_directory(farmctl.DATA_ROOT, 'persistent data root', create=True)
        existing = subprocess.run(['docker', 'volume', 'inspect', volume], capture_output=True, text=True)
        candidate_data_directory = farmctl.data_path(device)
        if candidate_data_directory.exists():
            data_directory = require_private_directory(candidate_data_directory,
                                                        f'{device} persistent data directory')
        elif existing.returncode == 0 or record.get('phase') != 'reserved':
            raise RuntimeError('managed persistent data directory is missing; restore it instead of creating an empty identity')
        else:
            data_directory = require_private_directory(data_root / device / 'data',
                                                        f'{device} persistent data directory', create=True)
        if created and existing.returncode == 0:
            raise RuntimeError('unmanaged device volume already exists; explicit migration required')
        if created:
            if existing.returncode == 0 or any(farmctl.inspect(f'{kind}-{device}') for kind in ('android', 'proxy', 'screen')):
                raise RuntimeError('unmanaged device already exists; explicit migration required')
        elif existing.returncode != 0 and record.get('phase') != 'reserved':
            raise RuntimeError('managed data volume is missing; restore it instead of creating an empty identity')
        if existing.returncode == 0:
            labels = json.loads(existing.stdout)[0].get('Labels') or {}
            if labels.get('farm.request') != record['request_hash']:
                raise RuntimeError('existing volume ownership mismatch')
            farmctl.validate_managed_volume(device, record)
        secret_path = args.secret_dir / f'{device}.json'
        if secret_path.exists():
            installed_secret = read_private_json(secret_path, 'installed proxy secret')
            installed_identity = json.dumps([installed_secret.get('type'), installed_secret.get('server'),
                                             installed_secret.get('server_port'), installed_secret.get('username')])
            if installed_identity != proxy_identity:
                raise RuntimeError('existing device proxy endpoint/session differs; refusing to rotate identity')
        if existing.returncode != 0:
            farmctl.run('docker', 'volume', 'create', '--label', f'farm.device={device}',
                        '--label', f'farm.request={record["request_hash"]}', '--driver', 'local',
                        '--opt', 'type=none', '--opt', f'device={data_directory}', '--opt', 'o=bind', volume)
        record['phase'] = 'volume_created'
        inventory.save(registry, inventory_state)
        atomic_json(secret_path, secret)
        record['phase'] = 'secret_installed'
        inventory.save(registry, inventory_state)
        record['phase'] = 'identity_baselining' if not record.get('identity_baseline_created') else 'starting'
        inventory.save(registry, inventory_state)
        try:
            farmctl.run(sys.executable, str(ROOT / 'ops/farmctl.py'), 'start', device,
                        '--compose', str(args.compose.resolve()), '--project', args.project,
                        '--secret-dir', str(args.secret_dir))
            observed = farmctl.run('docker', 'exec', f'proxy-{device}', 'curl', '--noproxy', '*',
                                  '-4', '-fsS', '--max-time', '15', 'https://api.ipify.org', capture=True).strip()
            if observed != expected_ip:
                raise RuntimeError('sticky proxy egress differs from approved IP')
            record['phase'] = 'installing_apk'
            inventory.save(registry, inventory_state)
            app_installer.install(device, artifact, permissions, activity)
            record.update(phase='ready_for_operator', apk_sha256=artifact['sha256'],
                          apk_package=artifact['package'], prepared_at=int(time.time()), egress_ip=observed)
            record.pop('last_error', None)
            inventory.save(registry, inventory_state)
            print(f'{device}: approved QA application installed and opened. Any vendor sign-in stays manual.')
        except BaseException:
            with contextlib.suppress(Exception):
                farmctl.run(sys.executable, str(ROOT / 'ops/farmctl.py'), 'stop', device)
            record.update(phase='failed', last_error='Preparation failed; inspect local operator output and retry identical request')
            inventory.save(registry, inventory_state)
            raise


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Do not print subprocess argv: it may contain configuration details.
        print(str(exc) if not isinstance(exc, subprocess.CalledProcessError) else 'External command failed; preparation stopped.', file=sys.stderr)
        sys.exit(1)
