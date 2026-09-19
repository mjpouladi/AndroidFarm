#!/usr/bin/env python3
"""Host-only operator entrypoint. Requires root, Docker Compose >= 2.33.1 and iptables backend."""
import argparse
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

try:
    from .resources import probe, admission, local_docker
    from .account_policy import assert_not_held
    from .identity import verify as verify_identity
    from . import inventory
    from .secureio import atomic_json, read_private_json, require_private_directory
except ImportError:
    from resources import probe, admission, local_docker
    from account_policy import assert_not_held
    from identity import verify as verify_identity
    import inventory
    from secureio import atomic_json, read_private_json, require_private_directory

MAX_ACTIVE = 10
DATA_ROOT = Path('/opt/farm/data/instances')


def run(*args, capture=False, timeout=300):
    return subprocess.run(args, check=True, text=True, capture_output=capture, timeout=timeout).stdout


def inspect(name):
    p = subprocess.run(['docker', 'inspect', name], text=True, capture_output=True)
    return json.loads(p.stdout)[0] if p.returncode == 0 else None


def active_devices():
    return run('docker', 'ps', '--filter', 'label=farm.role=android',
               '--format', '{{.Names}}', capture=True).splitlines()


def data_path(device):
    return DATA_ROOT / device / 'data'


def validate_managed_volume(device, record):
    """Require the named volume to bind the audited host data directory."""
    volume = f'redroid-data-{device}'
    info = json.loads(run('docker', 'volume', 'inspect', volume, capture=True))[0]
    labels = info.get('Labels') or {}
    if labels.get('farm.device') != device or labels.get('farm.request') != record.get('request_hash'):
        raise RuntimeError('managed data volume ownership labels do not match inventory')
    options = info.get('Options') or {}
    bind_options = {part.strip() for part in str(options.get('o', '')).split(',')}
    if (options.get('type') != 'none' or 'bind' not in bind_options or
            Path(str(options.get('device', ''))).resolve() != data_path(device).resolve()):
        raise RuntimeError('managed data volume is not bound to the audited persistent data directory')
    return info


def assert_capacity(active, device):
    if f'android-{device}' in active:
        raise RuntimeError('device is already running; stop before starting again')
    if len(active) >= MAX_ACTIVE:
        raise RuntimeError('maximum of 10 active Android containers reached')


def guard(device, secret_dir, allow_upstream=True):
    """Independent host egress guard; Android privileged namespace cannot flush it."""
    secret = read_private_json(secret_dir / f'{device}.json', 'installed proxy secret')
    ip = str(ipaddress.IPv4Address(secret['server']))
    if not ipaddress.ip_address(ip).is_global:
        raise RuntimeError('upstream must be a public IPv4')
    port = int(secret['server_port'])
    if not 1 <= port <= 65535:
        raise RuntimeError('invalid upstream port')
    index = int(device[3:])
    bridge, chain = f'br-af{index:03d}', f'AF{index:03d}'
    run('iptables', '-w', '-S', 'DOCKER-USER', capture=True)
    existing = subprocess.run(['iptables', '-w', '-S', chain], capture_output=True)
    if existing.returncode != 0:
        run('iptables', '-w', '-N', chain)
    # iptables-restore applies the flush and replacement as one table update.
    # A sequence of ``iptables -F`` / ``-A`` commands would briefly leave a
    # privileged Android namespace without a deny rule.
    rules = []
    if allow_upstream:
        rules.append(f'-A {chain} -p tcp -d {ip} --dport {port} -j RETURN')
    rules.append(f'-A {chain} -j DROP')
    payload = '\n'.join(['*filter', f'-F {chain}', *rules, 'COMMIT', ''])
    result = subprocess.run(['iptables-restore', '-w', '--noflush'], input=payload, text=True,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('failed to update host egress guard atomically')
    rule = ['-i', bridge, '-j', chain]
    # Preserve an existing jump while replacing the chain. Removing then
    # reinserting it would create a direct-egress window. Newly managed
    # bridges get their jump before any container joins the bridge.
    if subprocess.run(['iptables', '-w', '-C', 'DOCKER-USER', *rule], capture_output=True).returncode != 0:
        run('iptables', '-w', '-I', 'DOCKER-USER', '1', *rule)


def cut_egress(device):
    """Best-effort immediate host-level cut before graceful container shutdown."""
    index = int(device[3:])
    chain = f'AF{index:03d}'
    if subprocess.run(['iptables', '-w', '-S', chain], capture_output=True).returncode == 0:
        payload = '\n'.join(['*filter', f'-F {chain}', f'-A {chain} -j DROP', 'COMMIT', ''])
        result = subprocess.run(['iptables-restore', '-w', '--noflush'], input=payload, text=True,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('failed to close host egress guard atomically')


def stop(device):
    guard_error = None
    try:
        cut_egress(device)
    except BaseException as exc:
        guard_error = exc
    finally:
        for kind in ('screen', 'android', 'proxy'):
            name = f'{kind}-{device}'
            state = inspect(name)
            if state and state['State']['Running']:
                if state['State'].get('Paused'):
                    run('docker', 'unpause', name, timeout=30)
                run('docker', 'stop', '-t', '60', name)
    if guard_error:
        raise RuntimeError('host egress guard update failed; containers were still stopped') from guard_error


def check(device):
    for kind in ('proxy', 'android', 'screen'):
        item = inspect(f'{kind}-{device}')
        if not item or not item['State']['Running']:
            raise RuntimeError(f'{kind}-{device} is not running')
    run('docker', 'exec', f'proxy-{device}', '/healthcheck.sh')
    target = f'10.232.{int(device[3:])}.2:5555'
    boot = run('docker', 'exec', f'screen-{device}', 'adb', '-s', target,
               'shell', 'getprop', 'sys.boot_completed', capture=True).strip()
    if boot != '1':
        raise RuntimeError('Android boot not complete')
    print(f'{device}: proxy and ADB ready; verify browser rendering separately')


def _global_ipv4(text):
    matches = re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', text)
    valid = []
    for candidate in matches:
        try:
            address = ipaddress.IPv4Address(candidate)
            if address.is_global:
                valid.append(str(address))
        except ipaddress.AddressValueError:
            pass
    if not valid:
        raise RuntimeError('egress probe did not return a public IPv4 address')
    return valid[-1]


def proxy_egress_ip(device):
    result = run('docker', 'exec', f'proxy-{device}', 'curl', '--noproxy', '*', '-4', '-fsS',
                 '--max-time', '15', 'https://api.ipify.org', capture=True, timeout=25)
    return _global_ipv4(result)


def android_egress_ip(device):
    """Probe from an ADB shell UID, which traverses the transparent redirect rules."""
    target = f'10.232.{int(device[3:])}.2:5555'
    adb = ['docker', 'exec', f'screen-{device}', 'adb', '-s', target, 'shell']
    curl = subprocess.run([*adb, 'command', '-v', 'curl'], text=True, capture_output=True, timeout=15)
    if curl.returncode == 0 and curl.stdout.strip():
        result = run(*adb, 'curl', '-4', '-fsS', '--max-time', '15', 'https://api.ipify.org',
                     capture=True, timeout=25)
    else:
        request = "printf 'GET / HTTP/1.0\\r\\nHost: api.ipify.org\\r\\nConnection: close\\r\\n\\r\\n' | toybox nc -w 15 api.ipify.org 80"
        result = run(*adb, 'sh', '-c', request, capture=True, timeout=25)
    return _global_ipv4(result)


def wait_proxy(device, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(['docker', 'exec', f'proxy-{device}', '/healthcheck.sh'],
                                text=True, capture_output=True, timeout=15)
        if result.returncode == 0:
            return
        time.sleep(3)
    raise RuntimeError('proxy did not become healthy before timeout')


def validate_compose(config, device, secret_dir):
    """Attest the isolation-critical parts of the resolved Compose document.

    This is deliberately an allowlist. Redroid is privileged, so merely
    checking for one good network or mount would let an extra network, bind
    mount, or published port defeat the egress boundary.
    """
    services = config.get('services', {})
    proxy_name, android_name, screen_name = (f'{kind}-{device}' for kind in ('proxy', 'android', 'screen'))
    try:
        proxy = services[proxy_name]
        android = services[android_name]
        screen = services[screen_name]
    except KeyError as exc:
        raise RuntimeError(f'resolved Compose is missing {exc.args[0]}') from exc
    if android.get('network_mode') != f'service:{proxy_name}':
        raise RuntimeError('Compose drift: Android must share only its proxy network namespace')
    index = int(device[3:])
    egress, control = f'egress-{device}', f'control-{device}'
    if set(proxy.get('networks', {})) != {egress, control}:
        raise RuntimeError('Compose drift: proxy must have exactly its egress and internal control networks')
    if android.get('networks'):
        raise RuntimeError('Compose drift: Android must not declare additional networks')
    if set(screen.get('networks', {})) != {'coolify', control}:
        raise RuntimeError('Compose drift: screen must have exactly Coolify and its control network')
    proxy_networks, screen_networks = proxy['networks'], screen['networks']
    if (proxy_networks[egress].get('ipv4_address') != f'10.231.{index}.2' or
            proxy_networks[control].get('ipv4_address') != f'10.232.{index}.2' or
            screen_networks[control].get('ipv4_address') != f'10.232.{index}.3'):
        raise RuntimeError('Compose drift: control and egress endpoints do not match the audited addresses')
    networks = config.get('networks', {})
    try:
        egress_config, control_config, coolify_config = networks[egress], networks[control], networks['coolify']
    except KeyError as exc:
        raise RuntimeError(f'Compose drift: missing network {exc.args[0]}') from exc
    if (egress_config.get('driver', 'bridge') != 'bridge' or egress_config.get('internal') or
            egress_config.get('name') != f'farm-egress-{device}' or
            egress_config.get('driver_opts', {}).get('com.docker.network.bridge.name') != f'br-af{index:03d}'):
        raise RuntimeError('Compose drift: egress network does not match the host guard bridge')
    if (not control_config.get('internal') or control_config.get('name') != f'farm-control-{device}' or
            control_config.get('driver', 'bridge') != 'bridge'):
        raise RuntimeError('Compose drift: device control network must be the expected internal bridge')
    if not coolify_config.get('external'):
        raise RuntimeError('Compose drift: screen must use the pre-existing Coolify network')
    def subnet(network):
        ipam = network.get('ipam', {})
        entries = ipam.get('config', []) if isinstance(ipam, dict) else []
        return entries[0].get('subnet') if len(entries) == 1 and isinstance(entries[0], dict) else None
    if subnet(egress_config) != f'10.231.{index}.0/29' or subnet(control_config) != f'10.232.{index}.0/29':
        raise RuntimeError('Compose drift: device subnets do not match the audited topology')
    for service, name in ((proxy, proxy_name), (android, android_name), (screen, screen_name)):
        if service.get('container_name') != name or service.get('restart') != 'no':
            raise RuntimeError(f'Compose drift: {name} name or restart policy is unsafe')
        if service.get('pid') or service.get('devices'):
            raise RuntimeError(f'Compose drift: {name} may not share host PID namespace or devices')
    if proxy.get('privileged') or screen.get('privileged') or android.get('privileged') is not True:
        raise RuntimeError('Compose drift: only the Android service may be privileged')
    if set(proxy.get('cap_add', [])) != {'NET_ADMIN'} or set(proxy.get('cap_drop', [])) != {'NET_RAW'}:
        raise RuntimeError('Compose drift: proxy capability policy differs')
    if set(screen.get('cap_drop', [])) != {'ALL'} or 'no-new-privileges:true' not in screen.get('security_opt', []):
        raise RuntimeError('Compose drift: screen hardening policy differs')
    if proxy.get('volumes') or screen.get('volumes'):
        raise RuntimeError('Compose drift: proxy and screen may not mount host or persistent volumes')
    if android.get('ports') or screen.get('ports'):
        raise RuntimeError('Compose drift: only the proxy may publish the loopback ADB port')
    mounts = android.get('volumes', [])
    volume = f'redroid-data-{device}'
    if (len(mounts) != 1 or not isinstance(mounts[0], dict) or mounts[0].get('type') != 'volume' or
            mounts[0].get('target') != '/data' or mounts[0].get('source') != volume):
        raise RuntimeError('Compose drift: expected persistent device volume is not mounted at /data')
    command = android.get('command') or []
    required_properties = {f'androidboot.serialno=farm-{device}', 'androidboot.use_memfd=true',
                           'ro.product.brand=redroid', 'ro.product.manufacturer=remote-android',
                           'ro.product.model=Redroid QA Phone'}
    if not isinstance(command, list) or not required_properties.issubset(set(command)):
        raise RuntimeError('Compose drift: Android must keep the audited honest QA properties')
    volume_config = config.get('volumes', {}).get(volume, {})
    if not volume_config.get('external') or volume_config.get('name') != volume:
        raise RuntimeError('Compose drift: device data volume must be the audited external named volume')
    secrets = proxy.get('secrets', [])
    if len(secrets) != 1 or not isinstance(secrets[0], dict) or secrets[0].get('target') != 'proxy.json':
        raise RuntimeError('Compose drift: proxy must have exactly one secret')
    source = secrets[0]['source']
    configured = Path(config['secrets'][source]['file']).resolve()
    if configured != (secret_dir / f'{device}.json').resolve():
        raise RuntimeError('Compose drift: proxy secret path does not match managed secret directory')
    ports = proxy.get('ports', [])
    adb = [item for item in ports if isinstance(item, dict) and int(item.get('target', 0)) == 5555]
    if (len(adb) != 1 or adb[0].get('host_ip') != '127.0.0.1' or
            str(adb[0].get('protocol', 'tcp')).lower() != 'tcp' or
            int(adb[0].get('published', 0)) != 5550 + index):
        raise RuntimeError('Compose drift: ADB must be published once on IPv4 loopback only')
    labels = screen.get('labels', {})
    middleware = labels.get(f'traefik.http.routers.farm-{device}.middlewares', '')
    route = labels.get(f'traefik.http.routers.farm-{device}.rule', '')
    service_port = labels.get(f'traefik.http.services.farm-{device}.loadbalancer.server.port')
    if (str(labels.get('traefik.enable')).lower() != 'true' or 'farm-auth@file' not in middleware.split(',') or
            f'PathPrefix(`/d/{device}/`)' not in route or str(service_port) != '6080'):
        raise RuntimeError('Compose drift: screen route is missing the required auth middleware')


def backup(device, destination):
    record = inventory.find(inventory.load(), device)
    if not record:
        raise RuntimeError('device is not allocated in the managed inventory')
    volume = f'redroid-data-{device}'
    validate_managed_volume(device, record)
    stop(device)
    source = data_path(device).resolve(strict=True)
    if not source.is_dir():
        raise RuntimeError('backup refused: Docker volume mountpoint is not a directory')
    root = require_private_directory(destination.resolve(), 'backup root', create=True)
    folder = require_private_directory(root / device, f'{device} backup directory', create=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    final = folder / f'{stamp}.tar'
    partial = folder / f'{stamp}.tar.partial'
    manifest = folder / f'{stamp}.manifest.json'
    if final.exists() or partial.exists():
        raise RuntimeError('backup filename already exists')
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(descriptor)
    try:
        run('tar', '--numeric-owner', '--xattrs', '--acls', '--sparse', '-cpf', str(partial), '-C', str(source), '.', timeout=3600)
        os.chmod(partial, 0o600)
        checksum = _sha256(partial)
        size = partial.stat().st_size
        os.replace(partial, final)
        try:
            atomic_json(manifest, {'schema_version': 1, 'device': device, 'volume': volume,
                                   'created_at': stamp, 'filename': final.name, 'sha256': checksum,
                                   'size_bytes': size, 'image_data_backup': True})
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                final.unlink()
            raise
    except BaseException:
        # A 0600 .partial is evidence of an interrupted archive, never a
        # restore candidate. It is deliberately retained for diagnosis.
        raise
    print(f'Backup: {final}; manifest: {manifest}; device remains stopped')


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['start', 'stop', 'check', 'ip', 'backup', 'status'])
    p.add_argument('device', nargs='?')
    p.add_argument('--compose', default='docker-compose.farm.yml')
    p.add_argument('--project', default='android-farm-devices')
    p.add_argument('--secret-dir', type=Path, default=Path('/etc/android-farm/secrets'))
    p.add_argument('--backup-dir', type=Path, default=Path('/var/backups/android-farm'))
    args = p.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        p.error('run on the Ubuntu Docker host as root')
    local_docker()
    if args.action != 'status' and not re.fullmatch(r'num(?:0[1-9]|[1-9][0-9]|1[0-9]{2}|200)', args.device or ''):
        p.error('device must be num01..num200')
    import fcntl
    # One host-wide lock shared by all checkouts and projects, including backups.
    with open('/run/lock/android-farm.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = args.device
        if args.action == 'status':
            print('\n'.join(active_devices()))
        elif args.action == 'stop':
            stop(d)
        elif args.action == 'check':
            check(d)
        elif args.action == 'ip':
            print(json.dumps({'device': d, 'proxy_namespace': proxy_egress_ip(d),
                              'android_shell': android_egress_ip(d), 'checked_at': int(time.time())}))
        elif args.action == 'backup':
            backup(d, args.backup_dir)
        elif args.action == 'start':
            assert_not_held(d)
            state = inventory.load()
            record = inventory.find(state, d)
            if not record:
                raise RuntimeError('device is not allocated in the managed inventory')
            if record.get('phase') not in {*inventory.COMPLETE_PHASES, 'starting', 'identity_baselining'}:
                raise RuntimeError('device is not in a startable managed phase; resume it only through device-provisioner')
            expected_ip = record.get('expected_egress_ip') or record.get('egress_ip')
            try:
                expected_ip = str(ipaddress.IPv4Address(expected_ip))
            except (ipaddress.AddressValueError, TypeError):
                raise RuntimeError('device has no valid approved egress IP')
            if not ipaddress.ip_address(expected_ip).is_global:
                raise RuntimeError('approved egress IP must be public')
            validate_managed_volume(d, record)
            active = active_devices()
            assert_capacity(active, d)
            admission(probe(), len(active))
            compose = ['docker', 'compose', '-p', args.project, '-f', str(Path(args.compose).resolve()),
                       '--profile', 'manual']
            resolved = json.loads(run(*compose, 'config', '--format', 'json', capture=True))
            validate_compose(resolved, d, args.secret_dir)
            # First prove the gateway's endpoint while no Android namespace is
            # running. Then close the host guard before booting Android, so no
            # guest process can reach the upstream prior to the identity check.
            stop(d)
            guard(d, args.secret_dir, allow_upstream=True)
            android_paused = False
            try:
                run(*compose, 'up', '-d', '--no-deps', '--force-recreate', f'proxy-{d}')
                wait_proxy(d)
                if proxy_egress_ip(d) != expected_ip:
                    raise RuntimeError('approved sticky egress IP mismatch; device returned to stopped state')
                guard(d, args.secret_dir, allow_upstream=False)
                run(*compose, 'up', '-d', '--no-deps', '--force-recreate', f'android-{d}')
                run(*compose, 'up', '-d', '--no-deps', '--force-recreate', f'screen-{d}')
                verify_identity(d)
                assert_not_held(d)
                # Freeze all guest processes before the first permitted egress.
                # This lets the gateway prove its sticky public IP without any app
                # in the Android guest being able to send a concurrent request.
                run('docker', 'pause', f'android-{d}', timeout=30)
                android_paused = True
                guard(d, args.secret_dir, allow_upstream=True)
                wait_proxy(d)
                proxy_ip = proxy_egress_ip(d)
                if proxy_ip != expected_ip:
                    raise RuntimeError('approved sticky egress IP mismatch; device returned to stopped state')
                run('docker', 'unpause', f'android-{d}', timeout=30)
                android_paused = False
                android_ip = android_egress_ip(d)
                if android_ip != expected_ip:
                    raise RuntimeError('Android-shell egress IP mismatch; device returned to stopped state')
                print(f'{d} started; identity and egress verified. Use check and browser acceptance tests.')
            except BaseException:
                if android_paused:
                    with contextlib.suppress(Exception):
                        run('docker', 'unpause', f'android-{d}', timeout=30)
                with contextlib.suppress(Exception):
                    stop(d)
                raise


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
