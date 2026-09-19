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
    from .secureio import atomic_json, read_private_json, require_private_directory, require_private_file
    from .device_ids import canonical_device, device_index, network_plan
    from .device_profiles import (PROFILE_DIGEST_LABEL, REDROID_IMAGES, apply_profile,
                                  load as load_device_profile, parse_cpus, parse_memory_bytes)
    from .proxy_store import ProxyStore
    from . import adb_helper, desired_state, events
except ImportError:
    from resources import probe, admission, local_docker
    from account_policy import assert_not_held
    from identity import verify as verify_identity
    import inventory
    from secureio import atomic_json, read_private_json, require_private_directory, require_private_file
    from device_ids import canonical_device, device_index, network_plan
    from device_profiles import (PROFILE_DIGEST_LABEL, REDROID_IMAGES, apply_profile,
                                  load as load_device_profile, parse_cpus, parse_memory_bytes)
    from proxy_store import ProxyStore
    import adb_helper
    import desired_state
    import events

DATA_ROOT = Path('/opt/farm/data/instances')


def _record_intent(device, running):
    """Persist operator intent best-effort; a missing state file never blocks lifecycle work."""
    try:
        desired_state.set_running(device, running)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'desired state unavailable: {exc}', file=sys.stderr)


def crashed_and_wanted(device):
    """True when Android exited abnormally and the last recorded intent is running."""
    android = inspect(f'android-{device}')
    if not android:
        return False
    state = android.get('State') or {}
    if state.get('Running') or state.get('Paused') or state.get('Restarting'):
        return False
    exit_code = state.get('ExitCode')
    crashed = bool(state.get('OOMKilled')) or (isinstance(exit_code, int) and exit_code != 0)
    return crashed and desired_state.wants_running(device)


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


def validate_managed_proxy(device, record, secret_dir,
                           registry=Path('/var/lib/android-farm/proxies.json'),
                           store_dir=Path('/etc/android-farm/proxies')):
    """Attest registry assignment and installed credentials inside the lifecycle lock."""
    proxy_id = record.get('proxy_id')
    if record.get('egress') == 'direct':
        installed = read_private_json(Path(secret_dir) / f'{device}.json',
                                      f'{device} installed egress secret')
        if proxy_id or installed != {'type': 'direct'}:
            raise RuntimeError('direct egress device has an unexpected proxy configuration')
        return
    if not proxy_id:  # Read-only compatibility for legacy file-managed proxies.
        return
    store = ProxyStore(registry, store_dir)
    metadata = store.show(proxy_id)
    if metadata.get('state') != 'enabled' or metadata.get('assigned_device') != device:
        raise RuntimeError('managed proxy is disabled or no longer assigned to this device')
    if metadata.get('expected_egress_ip') != record.get('expected_egress_ip'):
        raise RuntimeError('managed proxy expected IP differs from the persistent allocation')
    installed = read_private_json(Path(secret_dir) / f'{device}.json',
                                  f'{device} installed proxy secret')
    current = store.provisioning_secret(proxy_id)
    keys = ('type', 'server', 'server_port', 'username', 'password')
    if any(installed.get(key) != current.get(key) for key in keys):
        raise RuntimeError('installed proxy credential differs from the managed registry')


def assert_capacity(active, device, report=None):
    if f'android-{device}' in active:
        raise RuntimeError('device is already running; stop before starting again')
    admission(report or probe(), len(active))


def guard(device, secret_dir, allow_upstream=True):
    """Independent host egress guard; Android privileged namespace cannot flush it."""
    secret = read_private_json(secret_dir / f'{device}.json', 'installed proxy secret')
    direct = secret.get('type') == 'direct'
    if direct:
        if secret != {'type': 'direct'}:
            raise RuntimeError('direct egress secret carries unexpected upstream fields')
        ip = port = None
    else:
        ip = str(ipaddress.IPv4Address(secret['server']))
        if not ipaddress.ip_address(ip).is_global:
            raise RuntimeError('upstream must be a public IPv4')
        port = int(secret['server_port'])
        if not 1 <= port <= 65535:
            raise RuntimeError('invalid upstream port')
    addresses = network_plan(device_index(device, aliases=False))
    bridge, chain = addresses['bridge'], addresses['iptables_chain']
    run('iptables', '-w', '-S', 'DOCKER-USER', capture=True)
    existing = subprocess.run(['iptables', '-w', '-S', chain], capture_output=True)
    if existing.returncode != 0:
        run('iptables', '-w', '-N', chain)
    # iptables-restore applies the flush and replacement as one table update.
    # A sequence of ``iptables -F`` / ``-A`` commands would briefly leave a
    # privileged Android namespace without a deny rule.
    rules = []
    if allow_upstream:
        # Direct host egress has no pinned upstream: the open guard passes the
        # bridge through, and a hold or stop still turns it into a full DROP.
        rules.append(f'-A {chain} -j RETURN' if direct else
                     f'-A {chain} -p tcp -d {ip} --dport {port} -j RETURN')
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
    chain = network_plan(device_index(device, aliases=False))['iptables_chain']
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
    # Every guarded stop clears the running intent, so the health controller
    # never "recovers" a device an operator, a hold or a failed start stopped.
    _record_intent(device, False)
    events.note('device-stopped', device)
    if guard_error:
        raise RuntimeError('host egress guard update failed; containers were still stopped') from guard_error


def check(device):
    for kind in ('proxy', 'android', 'screen'):
        item = inspect(f'{kind}-{device}')
        if not item or not item['State']['Running']:
            raise RuntimeError(f'{kind}-{device} is not running')
    run('docker', 'exec', f'proxy-{device}', '/healthcheck.sh')
    target = f"{network_plan(device_index(device, aliases=False))['proxy_control_ip']}:5555"
    boot = run('docker', 'exec', f'screen-{device}', 'adb', '-s', target,
               'shell', 'getprop', 'sys.boot_completed', capture=True).strip()
    if boot != '1':
        raise RuntimeError('Android boot not complete')
    print(f'{device}: proxy and ADB ready; verify browser rendering separately')


def recovery_android_is_active(device):
    """Re-check the on-demand intent while the caller owns the lifecycle lock."""
    current = inspect(f'android-{device}')
    return bool(current and current.get('State', {}).get('Running'))


def recover_existing_screen(device):
    """Start only an existing screen for a still-active, healthy proxy namespace."""
    android = inspect(f'android-{device}')
    proxy = inspect(f'proxy-{device}')
    screen = inspect(f'screen-{device}')
    if not android or not android.get('State', {}).get('Running'):
        print(f'ANDROID_FARM_RECOVERY_SKIPPED {device} android-not-running')
        return False
    if not proxy or not proxy.get('State', {}).get('Running'):
        print(f'ANDROID_FARM_RECOVERY_SKIPPED {device} proxy-not-running')
        return False
    if screen is None:
        raise RuntimeError('screen recovery refused: managed container is missing')
    if screen.get('State', {}).get('Running'):
        print(f'ANDROID_FARM_RECOVERY_SKIPPED {device} screen-already-running')
        return False
    assert_not_held(device)
    run('docker', 'exec', f'proxy-{device}', '/healthcheck.sh', timeout=30)
    run('docker', 'start', f'screen-{device}', timeout=60)
    print(f'{device} screen recovered from its existing reviewed container')
    return True


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


def host_egress_ip(timeout=15):
    """The host's own public IPv4: the reference for direct host egress."""
    import urllib.request
    try:
        with urllib.request.urlopen('https://api.ipify.org', timeout=timeout) as response:
            return _global_ipv4(response.read(64).decode('ascii', 'replace'))
    except OSError as exc:
        raise RuntimeError(f'host egress probe failed: {exc}') from None


def namespace_egress_ip(device, direct):
    """Public IPv4 the device namespace leaves through.

    A proxy sidecar answers for itself through its tunnel. A direct-egress
    namespace has no tunnel and, once Android owns its routing policy, the
    sidecar's probe is not the reference: the host address is.
    """
    return host_egress_ip() if direct else proxy_egress_ip(device)


def proxy_egress_ip(device):
    result = run('docker', 'exec', f'proxy-{device}', 'curl', '--noproxy', '*', '-4', '-fsS',
                 '--max-time', '15', 'https://api.ipify.org', capture=True, timeout=25)
    return _global_ipv4(result)


def android_egress_ip(device, attempts=12, sleep=time.sleep):
    """Probe from an ADB shell UID, which traverses the transparent redirect rules.

    Right after an unpause ADB can report the guest offline for a few seconds
    and the guest resolver may not answer yet; such transient failures are
    retried for about a minute, a persistent one still fails the start.
    """
    target = f"{network_plan(device_index(device, aliases=False))['proxy_control_ip']}:5555"
    adb = ['docker', 'exec', f'screen-{device}', 'adb', '-s', target, 'shell']
    last = None
    for attempt in range(attempts):
        try:
            curl = subprocess.run([*adb, 'command', '-v', 'curl'], text=True, capture_output=True, timeout=15)
            if curl.returncode == 0 and curl.stdout.strip():
                result = run(*adb, 'curl', '-4', '-fsS', '--max-time', '15', 'https://api.ipify.org',
                             capture=True, timeout=25)
            else:
                request = "printf 'GET / HTTP/1.0\\r\\nHost: api.ipify.org\\r\\nConnection: close\\r\\n\\r\\n' | toybox nc -w 15 api.ipify.org 80"
                result = run(*adb, 'sh', '-c', request, capture=True, timeout=25)
            return _global_ipv4(result)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                sleep(5)
    if isinstance(last, RuntimeError):
        raise last
    raise RuntimeError('Android-shell egress probe failed: ADB did not answer after the unpause') from last


def android_answers(device, timeout=15):
    """True when the guest's ADB shell answers and Android reports a completed boot."""
    target = f"{network_plan(device_index(device, aliases=False))['proxy_control_ip']}:5555"
    try:
        probe = subprocess.run(['docker', 'exec', f'screen-{device}', 'adb', '-s', target, 'shell',
                                'getprop', 'sys.boot_completed'], text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == '1'


def recovery_skip_reason(action, record, device):
    """Why an automatic recovery must leave this device alone right now, or None.

    Preparation owns a device until its phase is complete: a health-timer
    restart during a long first boot or the APK installation would break the
    provisioning run and mark the device failed. A stall observed while the
    lifecycle lock was busy is re-checked here; a guest that answers now is
    left running.
    """
    if action not in {'recover', 'recover-crashed', 'recover-screen'}:
        return None
    if not record:
        return 'not-allocated'
    if record.get('phase') not in inventory.COMPLETE_PHASES:
        return 'preparation-in-progress'
    if action == 'recover' and android_answers(device):
        return 'healthy-now'
    return None


def ensure_android_image(resolved, device):
    """Pull a missing Android image before the timed compose up.

    The first pull of a Redroid image is large and used to run inside the
    five-minute `compose up` budget, where a slow registry turned the first
    start into an opaque timeout. An image that is already present is kept
    as is: a moving tag is never refreshed behind an attested start.
    """
    image = resolved['services'][f'android-{device}']['image']
    if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=30).returncode == 0:
        return False
    print(f'{device}: pulling Android image {image}; the first pull can take several minutes', flush=True)
    try:
        run('docker', 'pull', '--quiet', image, timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Android image pull timed out after 30 minutes; check the host network and retry') from None
    return True


def wait_proxy(device, timeout=120):
    deadline = time.monotonic() + timeout
    last = ''
    while time.monotonic() < deadline:
        result = subprocess.run(['docker', 'exec', f'proxy-{device}', '/healthcheck.sh'],
                                text=True, capture_output=True, timeout=15)
        if result.returncode == 0:
            return
        # curl's own error (DNS, connect, timeout) names the failing layer.
        last = ' '.join(((result.stderr or '') + (result.stdout or '')).split())[:300] or f'exit {result.returncode}'
        time.sleep(3)
    raise RuntimeError(f'proxy did not become healthy before timeout (last check: {last or "not attempted"})')


# Bounded, read-only evidence from the device's shared network namespace and
# the host guard. Printed to stderr after a failed start, which the API keeps
# in the root-only host job log; nothing here reaches HTTP.
FORENSIC_COMMANDS = (
    ('resolv.conf', ['cat', '/etc/resolv.conf']),
    ('addresses', ['ip', '-4', 'addr']),
    ('rules', ['ip', '-4', 'rule']),
    ('routes', ['ip', '-4', 'route', 'show', 'table', 'all']),
    ('filter', ['iptables', '-w', '-S']),
    ('nat', ['iptables', '-w', '-t', 'nat', '-S']),
    ('dns', ['nslookup', 'api.ipify.org']),
    ('https-by-name', ['curl', '--noproxy', '*', '-4', '-sS', '--max-time', '8', '-o', '/dev/null',
                       '-w', '%{http_code} %{remote_ip}', 'https://api.ipify.org']),
    ('https-by-ip', ['curl', '--noproxy', '*', '-4', '-sS', '--max-time', '8', '-o', '/dev/null',
                     '-w', '%{http_code}', 'https://1.1.1.1/cdn-cgi/trace']),
)


def network_forensics(device, stream=sys.stderr, limit=2500, timeout=10):
    """Print what the namespace and the host guard look like at the moment of failure."""
    name = f'proxy-{device}'
    print(f'--- network forensics {device} ---', file=stream)
    state = inspect(name)
    commands = []
    if state and (state.get('State') or {}).get('Running'):
        commands.extend((label, ['docker', 'exec', name, *argv]) for label, argv in FORENSIC_COMMANDS)
    else:
        print('proxy container is not running; namespace evidence unavailable', file=stream)
    plan = network_plan(device_index(device, aliases=False))
    commands.extend((('host-docker-user', ['iptables', '-w', '-S', 'DOCKER-USER']),
                     ('host-guard', ['iptables', '-w', '-S', plan['iptables_chain']]),
                     ('host-forward-policy', ['iptables', '-w', '-S', 'FORWARD', '1']),
                     ('host-bridge-route', ['ip', '-4', 'route', 'show', 'dev', plan['bridge']])))
    for label, argv in commands:
        try:
            result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
            text, status = ((result.stdout or '') + (result.stderr or '')).strip(), result.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            text, status = str(exc), 'error'
        print(f'[{label}] exit={status}\n{text[:limit]}', file=stream)
    print(f'--- end forensics {device} ---', file=stream)


def validate_compose(config, device, secret_dir, expected_profile=None, access_mode='domain'):
    """Attest the isolation-critical parts of the resolved Compose document.

    This is deliberately an allowlist. Redroid is privileged, so merely
    checking for one good network or mount would let an extra network, bind
    mount, or published port defeat the egress boundary.
    """
    if access_mode not in {'domain', 'ip'}:
        raise RuntimeError('Compose validation requires domain or ip access mode')
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
    index = device_index(device, aliases=False)
    addresses = network_plan(index)
    egress, control = f'egress-{device}', f'control-{device}'
    if set(proxy.get('networks', {})) != {egress, control}:
        raise RuntimeError('Compose drift: proxy must have exactly its egress and internal control networks')
    if android.get('networks'):
        raise RuntimeError('Compose drift: Android must not declare additional networks')
    if set(screen.get('networks', {})) != {'coolify', control}:
        raise RuntimeError('Compose drift: screen must have exactly Coolify and its control network')
    proxy_networks, screen_networks = proxy['networks'], screen['networks']
    if (proxy_networks[egress].get('ipv4_address') != addresses['proxy_egress_ip'] or
            proxy_networks[control].get('ipv4_address') != addresses['proxy_control_ip'] or
            screen_networks[control].get('ipv4_address') != addresses['screen_control_ip']):
        raise RuntimeError('Compose drift: control and egress endpoints do not match the audited addresses')
    screen_environment = screen.get('environment', {})
    if screen_environment.get('ADB_TARGET') != f"{addresses['proxy_control_ip']}:5555":
        raise RuntimeError('Compose drift: screen ADB target differs from the private proxy namespace')
    expected_screen = ({'SCREEN_WIDTH': str(expected_profile.resolution.width),
                        'SCREEN_HEIGHT': str(expected_profile.resolution.height),
                        'SCREEN_FPS': str(expected_profile.fps)} if expected_profile else
                       {'SCREEN_WIDTH': '720', 'SCREEN_HEIGHT': '1280', 'SCREEN_FPS': '20'})
    if any(str(screen_environment.get(key)) != value for key, value in expected_screen.items()):
        raise RuntimeError('Compose drift: browser display geometry differs from Android profile')
    networks = config.get('networks', {})
    try:
        egress_config, control_config, coolify_config = networks[egress], networks[control], networks['coolify']
    except KeyError as exc:
        raise RuntimeError(f'Compose drift: missing network {exc.args[0]}') from exc
    if (egress_config.get('driver', 'bridge') != 'bridge' or egress_config.get('internal') or
            egress_config.get('name') != f'farm-egress-{device}' or
            egress_config.get('driver_opts', {}).get('com.docker.network.bridge.name') != addresses['bridge']):
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
    if subnet(egress_config) != addresses['egress_subnet'] or subnet(control_config) != addresses['control_subnet']:
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
                           'ro.product.brand=redroid', 'ro.product.manufacturer=remote-android'}
    if expected_profile is None:
        required_properties.add('ro.product.model=Redroid QA Phone')
    else:
        required_properties.update({
            f'androidboot.redroid_width={expected_profile.resolution.width}',
            f'androidboot.redroid_height={expected_profile.resolution.height}',
            f'androidboot.redroid_dpi={expected_profile.dpi}',
            f'androidboot.redroid_fps={expected_profile.fps}',
            f'ro.product.model={expected_profile.device_model}',
            f'ro.product.locale={expected_profile.locale}',
        })
        if (android.get('image') != REDROID_IMAGES[expected_profile.android_version] or
                android.get('labels', {}).get(PROFILE_DIGEST_LABEL) != expected_profile.digest):
            raise RuntimeError('Compose drift: Android QA profile image or digest differs')
        if expected_profile.cpus is not None:
            try:
                cpus = parse_cpus(android.get('cpus'))
                memory = parse_memory_bytes(android.get('mem_limit'))
            except ValueError:
                raise RuntimeError('Compose drift: Android resource ceiling is missing or unreadable') from None
            if cpus != expected_profile.cpus or memory != expected_profile.memory_mib * 1024 ** 2:
                raise RuntimeError('Compose drift: Android resource ceiling differs from the QA profile')
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
    if (len(ports) != 1 or len(adb) != 1 or adb[0].get('host_ip') != '127.0.0.1' or
            str(adb[0].get('protocol', 'tcp')).lower() != 'tcp' or
            int(adb[0].get('published', 0)) != addresses['adb_port']):
        raise RuntimeError('Compose drift: ADB must be the proxy\'s only published port and use IPv4 loopback')
    labels = screen.get('labels', {})
    middleware = labels.get(f'traefik.http.routers.farm-{device}.middlewares', '')
    route = labels.get(f'traefik.http.routers.farm-{device}.rule', '')
    service_port = labels.get(f'traefik.http.services.farm-{device}.loadbalancer.server.port')
    expected_traefik = 'true' if access_mode == 'domain' else 'false'
    if (str(labels.get('traefik.enable')).lower() != expected_traefik or
            'farm-auth@file' not in middleware.split(',') or
            f'PathPrefix(`/d/{device}/`)' not in route or str(service_port) != '6080'):
        raise RuntimeError('Compose drift: screen access mode, route, or auth middleware differs')


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


def remove(device, secret_dir, registry, store_dir, *, purge_data=False,
           state_dir=Path('/var/lib/android-farm'), profile_dir=Path('/etc/android-farm/device-profiles')):
    """Decommission a device: stop it, drop its containers, registry links and secrets.

    The inventory index stays monotonic, so the identifier is never reused. Persistent
    data (the bind-mounted directory and its named volume) is kept unless
    ``purge_data`` is set; a backup taken beforehand is the operator's decision.
    """
    device = canonical_device(device)
    inventory_path = state_dir / 'inventory.json'
    state = inventory.load(inventory_path)
    record = inventory.find(state, device)
    if not record:
        raise RuntimeError('device is not allocated in the managed inventory')
    try:
        stop(device)
    except RuntimeError as exc:
        # The chain is deleted below anyway; a guard failure must not leave a half-removed device.
        print(f'{device}: {exc}; continuing with removal', file=sys.stderr)
    removed = {'device': device, 'containers': [], 'networks': [], 'files': [], 'volume': None,
               'data_directory': None, 'proxy_released': None}
    for kind in ('screen', 'android', 'proxy'):
        name = f'{kind}-{device}'
        if inspect(name) is not None:
            run('docker', 'rm', '-f', name, timeout=120)
            removed['containers'].append(name)
    plan = network_plan(device_index(device, aliases=False))
    # Host guard: drop the per-device chain and its DOCKER-USER jump (best effort).
    subprocess.run(['iptables', '-w', '-D', 'DOCKER-USER', '-i', plan['bridge'], '-j', plan['iptables_chain']],
                   capture_output=True)
    for argv in (['iptables', '-w', '-F', plan['iptables_chain']], ['iptables', '-w', '-X', plan['iptables_chain']]):
        subprocess.run(argv, capture_output=True)
    for network in (f'farm-egress-{device}', f'farm-control-{device}'):
        if subprocess.run(['docker', 'network', 'inspect', network], capture_output=True).returncode == 0:
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True, timeout=60)
            removed['networks'].append(network)
    volume = f'redroid-data-{device}'
    if purge_data:
        if subprocess.run(['docker', 'volume', 'inspect', volume], capture_output=True).returncode == 0:
            run('docker', 'volume', 'rm', volume, timeout=120)
            removed['volume'] = volume
        directory = DATA_ROOT / device
        if directory.exists() and not directory.is_symlink():
            import shutil
            shutil.rmtree(directory)
            removed['data_directory'] = str(directory)
    for path in (Path(secret_dir) / f'{device}.json',
                 state_dir / 'device-overrides' / f'{device}.json',
                 *([state_dir / 'identities' / f'{device}.json'] if purge_data else []),
                 Path(profile_dir) / f'{device}.json' if purge_data else None):
        if path is not None and path.exists() and not path.is_symlink():
            path.unlink()
            removed['files'].append(str(path))
    # Registry links: the proxy becomes free for another device; hold and intent go away.
    proxy_id = record.get('proxy_id')
    if proxy_id:
        try:
            ProxyStore(registry, store_dir).unassign(proxy_id, device)
            removed['proxy_released'] = proxy_id
        except (KeyError, RuntimeError) as exc:
            print(f'{device}: proxy {proxy_id} was not released: {exc}', file=sys.stderr)
    holds_path = state_dir / 'holds.json'
    if holds_path.exists():
        holds = read_private_json(holds_path, 'safety holds')
        if isinstance(holds, dict) and device in holds:
            holds.pop(device)
            atomic_json(holds_path, holds)
    try:
        desired = desired_state.load(state_dir / 'desired-state.json')
        if device in desired['devices']:
            desired['devices'].pop(device)
            atomic_json(state_dir / 'desired-state.json', desired)
    except (OSError, RuntimeError):
        pass
    state['devices'].pop(device)
    inventory.save(inventory_path, state)
    events.note('device-removed', device, 'data purged' if purge_data else 'data kept')
    print(f"{device} removed; {'data purged' if purge_data else 'persistent data kept'}; "
          f"proxy {'released: ' + proxy_id if proxy_id else 'none'}")
    return removed


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['start', 'recover', 'recover-crashed', 'recover-screen', 'stop',
                                      'check', 'ip', 'backup', 'status', 'remove'])
    p.add_argument('device', nargs='?')
    p.add_argument('--compose', default='docker-compose.farm.yml')
    p.add_argument('--env-file', type=Path)
    p.add_argument('--project', default='android-farm-runtime')
    p.add_argument('--access-mode', choices=['domain', 'ip'], default='domain')
    p.add_argument('--secret-dir', type=Path, default=Path('/etc/android-farm/secrets'))
    p.add_argument('--proxy-registry', type=Path, default=Path('/var/lib/android-farm/proxies.json'))
    p.add_argument('--proxy-store-dir', type=Path, default=Path('/etc/android-farm/proxies'))
    p.add_argument('--profile-dir', type=Path, default=Path('/etc/android-farm/device-profiles'))
    p.add_argument('--backup-dir', type=Path, default=Path('/var/backups/android-farm'))
    p.add_argument('--purge-data', action='store_true',
                   help='with remove: also delete the persistent data, volume, identity baseline and profile')
    args = p.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        p.error('run on the Ubuntu Docker host as root')
    local_docker()
    if args.action != 'status':
        try:
            args.device = canonical_device(args.device)
        except ValueError as exc:
            p.error(str(exc))
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
            record = inventory.find(inventory.load(), d) or {}
            print(json.dumps({'device': d,
                              'proxy_namespace': namespace_egress_ip(d, record.get('egress') == 'direct'),
                              'android_shell': android_egress_ip(d), 'checked_at': int(time.time())}))
        elif args.action == 'backup':
            backup(d, args.backup_dir)
        elif args.action == 'remove':
            remove(d, args.secret_dir, args.proxy_registry, args.proxy_store_dir,
                   purge_data=args.purge_data, profile_dir=args.profile_dir)
        elif args.action == 'recover-screen':
            # Health recovery must never recreate a missing container or start
            # a screen after an operator intentionally stopped Android.  This
            # decision and the start happen under the same lifecycle lock.
            skipped = recovery_skip_reason(args.action, inventory.find(inventory.load(), d), d)
            if skipped:
                print(f'ANDROID_FARM_RECOVERY_SKIPPED {d} {skipped}')
                return
            recover_existing_screen(d)
        elif args.action in {'start', 'recover', 'recover-crashed'}:
            if args.action == 'recover' and not recovery_android_is_active(d):
                print(f'ANDROID_FARM_RECOVERY_SKIPPED {d} android-not-running')
                return
            if args.action == 'recover-crashed' and not crashed_and_wanted(d):
                # Re-checked under the lifecycle lock: an operator stop, a hold
                # or a completed recovery since the observation wins.
                print(f'ANDROID_FARM_RECOVERY_SKIPPED {d} not-crashed-or-not-wanted')
                return
            assert_not_held(d)
            state = inventory.load()
            record = inventory.find(state, d)
            if not record:
                raise RuntimeError('device is not allocated in the managed inventory')
            skipped = recovery_skip_reason(args.action, record, d)
            if skipped:
                print(f'ANDROID_FARM_RECOVERY_SKIPPED {d} {skipped}')
                return
            if record.get('phase') not in {*inventory.COMPLETE_PHASES, 'starting', 'identity_baselining'}:
                raise RuntimeError('device is not in a startable managed phase; resume it only through device-provisioner')
            # Direct host egress is unpinned unless the request named an IP;
            # proxy devices always start against their approved sticky IP.
            direct = record.get('egress') == 'direct'
            expected_ip = record.get('expected_egress_ip') or (None if direct else record.get('egress_ip'))
            if expected_ip is not None or not direct:
                try:
                    expected_ip = str(ipaddress.IPv4Address(expected_ip))
                except (ipaddress.AddressValueError, TypeError):
                    raise RuntimeError('device has no valid approved egress IP')
                if not ipaddress.ip_address(expected_ip).is_global:
                    raise RuntimeError('approved egress IP must be public')
            validate_managed_proxy(d, record, args.secret_dir,
                                   args.proxy_registry, args.proxy_store_dir)
            validate_managed_volume(d, record)
            active = active_devices()
            if f'android-{d}' in active:
                # A caller may be reconciling a partially healthy instance or
                # replaying an interrupted start. Stop this device first, then
                # calculate capacity from the remaining fleet.
                stop(d)
                active = active_devices()
            assert_capacity(active, d, probe())
            compose = ['docker', 'compose']
            if args.env_file:
                compose.extend(['--env-file', str(args.env_file.resolve())])
            compose.extend(['-p', args.project, '-f', str(Path(args.compose).resolve())])
            expected_profile = None
            profile_path = args.profile_dir.resolve() / f'{d}.json'
            if profile_path.exists():
                require_private_file(profile_path, f'{d} QA profile')
                expected_profile = load_device_profile(profile_path)
                base_resolved = json.loads(run(*compose, '--profile', 'manual', 'config',
                                               '--format', 'json', capture=True))
                profiled = apply_profile(base_resolved, d, expected_profile)
                android_name = f'android-{d}'
                screen_name = f'screen-{d}'
                android = profiled['services'][android_name]
                screen_environment = profiled['services'][screen_name]['environment']
                override_root = require_private_directory(Path('/var/lib/android-farm/device-overrides'),
                                                          'device override directory', create=True)
                override_path = override_root / f'{d}.json'
                android_override = {'image': android['image'], 'command': android['command'],
                                    'labels': {PROFILE_DIGEST_LABEL: expected_profile.digest}}
                if expected_profile.cpus is not None:
                    android_override.update(cpus=android['cpus'], mem_limit=android['mem_limit'])
                atomic_json(override_path, {'services': {
                    android_name: android_override,
                    screen_name: {'environment': {
                        'SCREEN_WIDTH': screen_environment['SCREEN_WIDTH'],
                        'SCREEN_HEIGHT': screen_environment['SCREEN_HEIGHT'],
                        'SCREEN_FPS': screen_environment['SCREEN_FPS'],
                    }},
                }})
                compose.extend(['-f', str(override_path)])
            compose.extend(['--profile', 'manual'])
            resolved = json.loads(run(*compose, 'config', '--format', 'json', capture=True))
            validate_compose(resolved, d, args.secret_dir, expected_profile, args.access_mode)
            # Proxy and screen use local reviewed Dockerfiles with stable image
            # names. Explicitly build from the active immutable release so an
            # upgrade can never reuse an older local tag silently.
            run(*compose, 'build', f'proxy-{d}', f'screen-{d}', timeout=1200)
            # First prove the gateway's endpoint while no Android namespace is
            # running. Then close the host guard before booting Android, so no
            # guest process can reach the upstream prior to the identity check.
            stop(d)
            guard(d, args.secret_dir, allow_upstream=True)
            android_paused = False
            try:
                run(*compose, 'up', '-d', '--no-deps', '--force-recreate', f'proxy-{d}')
                wait_proxy(d)
                first_ip = proxy_egress_ip(d)
                if expected_ip is not None and first_ip != expected_ip:
                    raise RuntimeError('approved sticky egress IP mismatch; device returned to stopped state')
                guard(d, args.secret_dir, allow_upstream=False)
                ensure_android_image(resolved, d)
                run(*compose, 'up', '-d', '--no-deps', '--force-recreate', f'android-{d}', timeout=600)
                run(*compose, 'up', '-d', '--no-deps', '--force-recreate', f'screen-{d}')
                verify_identity(d)
                assert_not_held(d)
                if direct:
                    # Direct host egress: there is no sticky upstream to prove and the
                    # sidecar runs no tunnel. Once Android owns the namespace's routing
                    # policy the sidecar's own probe stops being meaningful, so the
                    # attestation is Android's shell leaving through the host address
                    # that the namespace showed before boot.
                    guard(d, args.secret_dir, allow_upstream=True)
                    android_ip = android_egress_ip(d)
                    if android_ip != (expected_ip or first_ip):
                        raise RuntimeError('Android-shell egress IP mismatch; device returned to stopped state')
                else:
                    # Freeze all guest processes before the first permitted egress.
                    # This lets the gateway prove its sticky public IP without any app
                    # in the Android guest being able to send a concurrent request.
                    run('docker', 'pause', f'android-{d}', timeout=30)
                    android_paused = True
                    guard(d, args.secret_dir, allow_upstream=True)
                    wait_proxy(d)
                    proxy_ip = proxy_egress_ip(d)
                    if proxy_ip != (expected_ip or first_ip):
                        raise RuntimeError('approved sticky egress IP mismatch; device returned to stopped state')
                    run('docker', 'unpause', f'android-{d}', timeout=30)
                    android_paused = False
                    android_ip = android_egress_ip(d)
                    if android_ip != proxy_ip:
                        raise RuntimeError('Android-shell egress IP mismatch; device returned to stopped state')
                if expected_profile is not None and expected_profile.timezone:
                    # The timezone is a persisted guest property; an unchanged
                    # value is only verified, a change restarts the framework.
                    # A guest that refuses the property does not undo a start
                    # whose identity and egress checks already passed: the
                    # failure is printed and logged as an event instead.
                    try:
                        environment = adb_helper.apply_environment(d, expected_profile)
                    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                        print(f'{d} timezone {expected_profile.timezone} was not applied: {exc}', file=sys.stderr)
                        events.note('environment-failed', d, f'timezone {expected_profile.timezone} not applied')
                    else:
                        if environment['applied']:
                            print(f"{d} environment applied: {', '.join(sorted(environment['applied']))}")
                            events.note('environment-applied', d, ', '.join(sorted(environment['applied'])))
                _record_intent(d, True)
                events.note('device-started', d, 'attested start' if args.action == 'start' else f'{args.action} recovery')
                print(f'{d} started; identity and egress verified. Use check and browser acceptance tests.')
            except BaseException:
                if args.action == 'start':
                    # Evidence first, while the namespace still exists; then the stop.
                    with contextlib.suppress(Exception):
                        network_forensics(d)
                if android_paused:
                    with contextlib.suppress(Exception):
                        run('docker', 'unpause', f'android-{d}', timeout=30)
                with contextlib.suppress(Exception):
                    stop(d)
                raise


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        # A captured child's own error text (e.g. curl's) is the actual diagnosis.
        detail = getattr(exc, 'stderr', None) or getattr(exc, 'output', None)
        if isinstance(detail, str) and detail.strip():
            print('child output: ' + ' '.join(detail.split())[-400:], file=sys.stderr)
        sys.exit(1)
