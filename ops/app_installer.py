"""Verify and install an approved QA APK on a managed Redroid instance."""
import hashlib
import re
from pathlib import Path
import subprocess
import time

try:
    from .secureio import read_private_json, require_private_file
except ImportError:  # direct host execution during recovery only
    from secureio import read_private_json, require_private_file

ALLOWED_GRANTS = {
    'android.permission.CAMERA',
    'android.permission.READ_CONTACTS',
    'android.permission.RECORD_AUDIO',
}
PACKAGE_RE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+')
ACTIVITY_RE = re.compile(r'(?:[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*/)?\.?[A-Za-z][A-Za-z0-9_.$]*')


def output(*args, timeout=60):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def digest(value):
    if not isinstance(value, str):
        raise ValueError('SHA-256 must be a string')
    value = value.replace(':', '').lower()
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('SHA-256 must contain 64 hexadecimal digits')
    return value


def approved_signers(policy_file, package):
    policy = read_private_json(policy_file, 'APK trust policy')
    try:
        signers = policy['packages'][package]['signers']
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f'package {package} is absent from the APK trust policy') from exc
    if not isinstance(signers, list) or not signers:
        raise RuntimeError('APK trust policy must contain at least one signer')
    return {digest(value) for value in signers}


def verify(path, expected_sha256, expected_package, policy_file):
    if not isinstance(expected_package, str) or not PACKAGE_RE.fullmatch(expected_package):
        raise ValueError('invalid Android package name')
    path = require_private_file(path, 'APK artifact').resolve(strict=True)
    if path.stat().st_size > 500 * 1024 ** 2:
        raise RuntimeError('APK exceeds the 500 MiB operator limit')
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual != digest(expected_sha256):
        raise RuntimeError('APK content hash mismatch')
    signature = output('apksigner', 'verify', '--print-certs', str(path))
    actual_signers = {value.lower() for value in
                      re.findall(r'Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]+)', signature)}
    trusted = approved_signers(policy_file, expected_package)
    if not actual_signers or not actual_signers.issubset(trusted):
        raise RuntimeError('APK signer is not allowed by the independent trust policy')
    badging = output('aapt', 'dump', 'badging', str(path))
    package = re.search(r"^package: name='([^']+)'", badging, re.M)
    if not package or package.group(1) != expected_package:
        raise RuntimeError('APK package name does not match the requested application')
    sdk = re.search(r"^sdkVersion:'(\d+)'", badging, re.M)
    native = re.search(r'^native-code:(.*)$', badging, re.M)
    return {'path': str(path), 'sha256': actual, 'package': expected_package,
            'sdk': int(sdk.group(1)) if sdk else 1,
            'abis': re.findall(r"'([^']+)'", native.group(1)) if native else []}


def install(device, artifact, permissions=(), activity=None, timeout=300):
    invalid = set(permissions) - ALLOWED_GRANTS
    if invalid:
        raise ValueError('unsupported runtime permission: ' + ', '.join(sorted(invalid)))
    if activity and not ACTIVITY_RE.fullmatch(activity):
        raise ValueError('invalid Android activity component')
    target = f'10.232.{int(device[3:])}.2:5555'
    adb = ['docker', 'exec', f'screen-{device}', 'adb', '-s', target]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if output(*adb, 'shell', 'getprop', 'sys.boot_completed', timeout=20).strip() == '1':
                break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
    else:
        raise RuntimeError('Android boot timed out before APK installation')
    serial = output(*adb, 'shell', 'getprop', 'ro.serialno', timeout=20).strip()
    if serial != f'farm-{device}':
        raise RuntimeError('device identity mismatch before APK installation')
    sdk = int(output(*adb, 'shell', 'getprop', 'ro.build.version.sdk', timeout=20).strip())
    abis = output(*adb, 'shell', 'getprop', 'ro.product.cpu.abilist', timeout=20).strip().split(',')
    if sdk < artifact['sdk'] or (artifact['abis'] and not set(artifact['abis']).intersection(abis)):
        raise RuntimeError(f'APK incompatible with Android SDK {sdk}, ABI {abis}')
    remote = '/tmp/farm-approved.apk'
    subprocess.run(['docker', 'cp', artifact['path'], f'screen-{device}:{remote}'], check=True, timeout=120)
    try:
        subprocess.run(['docker', 'exec', '-u', '0', f'screen-{device}', 'chmod', '0444', remote],
                       check=True, timeout=20)
        copied = output('docker', 'exec', f'screen-{device}', 'sha256sum', remote, timeout=30).split()[0]
        if copied != artifact['sha256']:
            raise RuntimeError('staged APK hash mismatch')
        result = output(*adb, 'install', '-r', remote, timeout=300)
        if 'Success' not in result:
            raise RuntimeError('Android package installation failed')
        output(*adb, 'shell', 'pm', 'path', artifact['package'], timeout=20)
        for permission in permissions:
            output(*adb, 'shell', 'pm', 'grant', artifact['package'], permission, timeout=20)
        if activity:
            component = activity if '/' in activity else f'{artifact["package"]}/{activity}'
            output(*adb, 'shell', 'am', 'start', '-n', component, timeout=30)
        else:
            output(*adb, 'shell', 'monkey', '-p', artifact['package'],
                   '-c', 'android.intent.category.LAUNCHER', '1', timeout=30)
    finally:
        subprocess.run(['docker', 'exec', '-u', '0', f'screen-{device}', 'rm', '-f', remote],
                       check=False, timeout=20)
