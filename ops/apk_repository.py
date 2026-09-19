"""Private APK repository: import once, verify, and reuse for every installation.

An operator uploads an APK (for example the official WhatsApp build) through the
console or the CLI.  The file is copied into a root-private, content-addressed
repository, inspected with ``aapt`` and ``apksigner``, registered in the
application catalog (``/etc/android-farm/apps.json``) and its signing
certificate is pinned in the independent trust policy
(``/etc/android-farm/apk-trust.json``).  Later provisioning runs re-verify the
stored file's hash and signer, so the same upload serves every future device.

Trust is anchored on first import: a later upload of the same package signed
by a different certificate is refused unless the operator explicitly allows the
rotation after checking the publisher's fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

try:
    from .app_installer import ACTIVITY_RE, ALLOWED_GRANTS, PACKAGE_RE, digest as certificate_digest
    from .secureio import atomic_json, read_private_json, require_private_directory
except ImportError:  # direct host execution
    from app_installer import ACTIVITY_RE, ALLOWED_GRANTS, PACKAGE_RE, digest as certificate_digest
    from secureio import atomic_json, read_private_json, require_private_directory


REPOSITORY = Path('/var/lib/android-farm/apk-repository')
CATALOG = Path('/etc/android-farm/apps.json')
TRUST_POLICY = Path('/etc/android-farm/apk-trust.json')
MAX_SIZE = 500 * 1024 ** 2
IDENTIFIER_RE = re.compile(r'[a-z][a-z0-9-]{2,62}\Z')
SHA256_RE = re.compile(r'[0-9a-f]{64}\Z')
CATALOG_REQUIRED = frozenset({'id', 'label', 'apk_path', 'apk_sha256', 'apk_package'})
CATALOG_OPTIONAL = frozenset({'apk_activity', 'apk_permissions', 'apk_version_name', 'apk_version_code',
                              'apk_signers', 'imported_at', 'source_filename'})
_STORED_NAME_RE = re.compile(r'[0-9a-f]{64}\.apk\Z')
ZIP_MAGIC = b'PK\x03\x04'


class RepositoryError(RuntimeError):
    """Operator-facing, secret-free import failure."""


def _tool(*argv, timeout=120):
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def _text(value, field, maximum=80):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum or
            any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f'invalid {field}')
    return value.strip()


def derive_identifier(package):
    """Turn ``com.whatsapp`` into ``whatsapp``; always a valid catalog identifier."""
    if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
        raise ValueError('invalid Android package name')
    candidate = re.sub(r'[^a-z0-9-]+', '-', package.rsplit('.', 1)[-1].lower()).strip('-')
    if not candidate or not candidate[0].isalpha():
        candidate = 'app-' + candidate if candidate else 'app'
    if len(candidate) < 3:
        candidate = (candidate + '-app')[:63]
    return candidate[:63]


def validate_catalog(value):
    """Validate the private application catalog and return entries keyed by id."""
    if not isinstance(value, dict) or set(value) != {'schema_version', 'apps'}:
        raise ValueError('unsupported request fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or not isinstance(value['apps'], list):
        raise ValueError('invalid application catalog schema')
    if len(value['apps']) > 256:
        raise ValueError('application catalog exceeds 256 entries')
    entries = {}
    for app in value['apps']:
        if not isinstance(app, dict) or set(app) - CATALOG_REQUIRED - CATALOG_OPTIONAL:
            raise ValueError('unsupported request fields')
        if CATALOG_REQUIRED - set(app):
            raise ValueError('required request fields are missing')
        identifier = app['id']
        if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError('invalid identifier')
        if identifier in entries:
            raise ValueError('duplicate application identifier')
        _text(app['label'], 'application label')
        path = Path(_text(app['apk_path'], 'APK path', 4096))
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('APK path must be absolute without traversal')
        package = app['apk_package']
        if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
            raise ValueError('invalid Android package name')
        sha256 = app['apk_sha256']
        if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', sha256):
            raise ValueError('invalid APK SHA-256')
        activity = app.get('apk_activity')
        if activity is not None and (not isinstance(activity, str) or not ACTIVITY_RE.fullmatch(activity)):
            raise ValueError('invalid Android activity')
        permissions = app.get('apk_permissions', [])
        if (not isinstance(permissions, list) or len(permissions) > 3 or
                any(not isinstance(p, str) or p not in ALLOWED_GRANTS for p in permissions) or
                len(set(permissions)) != len(permissions)):
            raise ValueError('unsupported Android runtime permissions')
        for key in ('apk_version_name', 'source_filename'):
            if key in app and app[key] is not None:
                _text(app[key], key, 160)
        if 'apk_version_code' in app and app['apk_version_code'] is not None and (
                isinstance(app['apk_version_code'], bool) or not isinstance(app['apk_version_code'], int)
                or app['apk_version_code'] < 0):
            raise ValueError('invalid APK version code')
        if 'imported_at' in app and app['imported_at'] is not None and (
                isinstance(app['imported_at'], bool) or not isinstance(app['imported_at'], int)):
            raise ValueError('invalid import timestamp')
        signers = app.get('apk_signers', [])
        if (not isinstance(signers, list) or len(signers) > 8 or
                any(not isinstance(s, str) or not SHA256_RE.fullmatch(s) for s in signers)):
            raise ValueError('invalid APK signer list')
        entries[identifier] = dict(app, apk_sha256=sha256.lower(), apk_permissions=permissions)
    return entries


def load_catalog(path=CATALOG):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return {}
    return validate_catalog(read_private_json(path, 'application catalog'))


def save_catalog(path, entries):
    apps = [entries[key] for key in sorted(entries)]
    validate_catalog({'schema_version': 1, 'apps': apps})
    atomic_json(path, {'schema_version': 1, 'apps': apps})


def load_trust(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return {'packages': {}}
    policy = read_private_json(path, 'APK trust policy')
    if not isinstance(policy, dict) or not isinstance(policy.get('packages'), dict):
        raise RepositoryError('APK trust policy structure is invalid')
    return policy


def inspect_apk(path, runner=_tool):
    """Read package, version, label, launcher activity and signers from a stored APK."""
    badging = runner('aapt', 'dump', 'badging', str(path))
    package = re.search(r"^package: name='([^']+)'", badging, re.M)
    if not package or not PACKAGE_RE.fullmatch(package.group(1)):
        raise RepositoryError('the file is not a readable Android package')
    version_name = re.search(r"versionName='([^']*)'", badging)
    version_code = re.search(r"versionCode='([0-9]+)'", badging)
    label = re.search(r"^application-label:'([^']*)'", badging, re.M)
    activity = re.search(r"^launchable-activity: name='([^']+)'", badging, re.M)
    sdk = re.search(r"^sdkVersion:'(\d+)'", badging, re.M)
    signature = runner('apksigner', 'verify', '--print-certs', str(path))
    signers = sorted({value.lower() for value in
                      re.findall(r'Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})', signature)})
    if not signers:
        raise RepositoryError('the APK has no verifiable signing certificate')
    launchable = activity.group(1) if activity and ACTIVITY_RE.fullmatch(activity.group(1)) else None
    return {'package': package.group(1),
            'version_name': (version_name.group(1)[:160] if version_name else None) or None,
            'version_code': int(version_code.group(1)) if version_code else None,
            'label': (label.group(1).strip()[:80] if label else '') or None,
            'activity': launchable, 'sdk': int(sdk.group(1)) if sdk else None,
            'signers': signers}


def _check_source(source):
    source = Path(source)
    info = source.lstat()
    if source.is_symlink() or not source.is_file():
        raise RepositoryError('APK source must be a regular file')
    if os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022):
        raise RepositoryError('APK source must be root-owned and not writable by group or others')
    if not 0 < info.st_size <= MAX_SIZE:
        raise RepositoryError('APK size must be between 1 byte and 500 MiB')
    with source.open('rb') as stream:
        if stream.read(4) != ZIP_MAGIC:
            raise RepositoryError('the file is not an APK (ZIP) archive')
    return source


def _store(source, repository):
    """Copy the source into the content-addressed repository; return (path, sha256)."""
    repository = require_private_directory(repository, 'APK repository', create=True)
    hasher = hashlib.sha256()
    descriptor, temporary_name = tempfile.mkstemp(prefix='.import-', suffix='.apk', dir=repository)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as output, Path(source).open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        sha256 = hasher.hexdigest()
        final = repository / f'{sha256}.apk'
        if final.exists() and not final.is_symlink() and final.stat().st_size == temporary.stat().st_size:
            temporary.unlink()  # Already imported: the repository is content-addressed.
        else:
            os.replace(temporary, final)
        return final, sha256
    finally:
        if temporary.exists():
            temporary.unlink()


def prune(repository, entries):
    """Delete stored APKs that no catalog entry references any more."""
    repository = Path(repository)
    if not repository.exists():
        return []
    referenced = {Path(app['apk_path']).resolve() for app in entries.values()}
    removed = []
    for candidate in repository.iterdir():
        if candidate.is_symlink() or not candidate.is_file() or not _STORED_NAME_RE.fullmatch(candidate.name):
            continue
        if candidate.resolve() not in referenced:
            candidate.unlink()
            removed.append(candidate.name)
    return removed


def import_apk(source, *, repository=REPOSITORY, catalog_path=CATALOG, trust_path=TRUST_POLICY,
               label=None, app_id=None, permissions=(), activity=None, source_filename=None,
               allow_signer_change=False, runner=_tool, clock=time.time):
    """Import one APK; idempotent for the same bytes, refuses silent signer changes."""
    permissions = list(permissions or [])
    invalid = set(permissions) - ALLOWED_GRANTS
    if invalid or len(set(permissions)) != len(permissions) or len(permissions) > 3:
        raise ValueError('unsupported runtime permission: ' + ', '.join(sorted(invalid)) if invalid
                         else 'permissions must be unique and at most three')
    if activity is not None and (not isinstance(activity, str) or not ACTIVITY_RE.fullmatch(activity)):
        raise ValueError('invalid Android activity component')
    if label is not None:
        label = _text(label, 'application label')
    if source_filename is not None:
        source_filename = _text(source_filename, 'source filename', 160)
    source = _check_source(source)
    stored, sha256 = _store(source, repository)
    try:
        metadata = inspect_apk(stored, runner)
        package = metadata['package']
        identifier = app_id if app_id is not None else derive_identifier(package)
        if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError('invalid identifier')
        # Trust policy: pin the signer on first import, refuse silent rotation later.
        policy = load_trust(trust_path)
        packages = policy['packages']
        recorded = packages.get(package)
        trusted = set()
        if isinstance(recorded, dict) and isinstance(recorded.get('signers'), list):
            trusted = {certificate_digest(value) for value in recorded['signers'] if isinstance(value, str)
                       and re.fullmatch(r'[0-9a-fA-F:]{64,95}', value)}
        actual = set(metadata['signers'])
        now = int(clock())
        signer_changed = bool(trusted) and not actual.issubset(trusted)
        if signer_changed and not allow_signer_change:
            raise RepositoryError(
                'APK signer differs from the certificate trusted for this package; verify the publisher '
                'fingerprint, then re-import with an explicit signer change')
        if not trusted or signer_changed:
            entry = {'signers': sorted(actual), 'trusted_at': now}
            if signer_changed:
                entry['previous_signers'] = sorted(trusted)
            packages[package] = entry
            atomic_json(trust_path, policy)
        # Catalog: one entry per identifier; a re-import replaces the version in place.
        entries = load_catalog(catalog_path)
        existing = entries.get(identifier)
        if existing and existing['apk_package'] != package:
            raise RepositoryError(f'identifier {identifier} already belongs to package {existing["apk_package"]}')
        entry = {
            'id': identifier,
            'label': label or (existing or {}).get('label') or metadata['label'] or package,
            'apk_path': str(stored), 'apk_sha256': sha256, 'apk_package': package,
            'apk_permissions': permissions if permissions or not existing else existing.get('apk_permissions', []),
            'apk_version_name': metadata['version_name'], 'apk_version_code': metadata['version_code'],
            'apk_signers': metadata['signers'], 'imported_at': now,
            'source_filename': source_filename or Path(source).name[:160],
        }
        chosen_activity = activity or (existing or {}).get('apk_activity') or metadata['activity']
        if chosen_activity:
            entry['apk_activity'] = chosen_activity
        entries[identifier] = entry
        save_catalog(catalog_path, entries)
        removed = prune(repository, entries)
    except BaseException:
        # Never leave an unreferenced artifact behind on a failed import.
        try:
            if not any(app.get('apk_sha256') == sha256 for app in load_catalog(catalog_path).values()):
                stored.unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError):
            pass
        raise
    return {'id': identifier, 'package': package, 'label': entry['label'], 'sha256': sha256,
            'version_name': metadata['version_name'], 'version_code': metadata['version_code'],
            'signers': metadata['signers'], 'signer_changed': signer_changed,
            'activity': entry.get('apk_activity'), 'permissions': entry['apk_permissions'],
            'path': str(stored), 'pruned': removed}


def list_apps(catalog_path=CATALOG):
    return [{'id': app['id'], 'label': app['label'], 'package': app['apk_package'],
             'version_name': app.get('apk_version_name'), 'version_code': app.get('apk_version_code'),
             'sha256': app['apk_sha256'], 'signers': app.get('apk_signers', []),
             'imported_at': app.get('imported_at'), 'path': app['apk_path']}
            for app in load_catalog(catalog_path).values()]


def remove_app(app_id, *, catalog_path=CATALOG, repository=REPOSITORY):
    entries = load_catalog(catalog_path)
    if app_id not in entries:
        raise KeyError('application does not exist')
    del entries[app_id]
    save_catalog(catalog_path, entries)
    return {'id': app_id, 'removed': True, 'pruned': prune(repository, entries)}


def stage_upload(stream, length, directory, *, maximum=MAX_SIZE, clock=time.time):
    """Persist an authenticated upload into a private staging directory.

    Returns the staging token.  Stale staged files older than a day are removed so
    an interrupted queue never accumulates unreviewed archives.
    """
    directory = require_private_directory(directory, 'upload staging directory', create=True)
    now = clock()
    for stale in directory.glob('*.apk'):
        try:
            if not stale.is_symlink() and stale.is_file() and now - stale.stat().st_mtime > 86400:
                stale.unlink()
        except OSError:
            pass
    if isinstance(length, bool) or not isinstance(length, int) or not 0 < length <= maximum:
        raise ValueError('upload size is outside the accepted range')
    token = os.urandom(16).hex()
    path = directory / f'{token}.apk'
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    received = 0
    try:
        with os.fdopen(descriptor, 'wb') as output:
            first = b''
            while received < length:
                block = stream.read(min(1024 * 1024, length - received))
                if not block:
                    break
                if received == 0:
                    first = block[:4]
                received += len(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if received != length:
            raise ValueError('upload ended before the announced size')
        if first != ZIP_MAGIC:
            raise ValueError('uploaded file is not an APK archive')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return token
