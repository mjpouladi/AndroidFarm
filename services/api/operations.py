"""Narrow host-side operations exposed by the authenticated farm API.

The browser supplies identifiers, never executable arguments, APK paths or a
Docker specification. All lifecycle work still crosses the provisioner's host
guards. Subprocess output is bounded and never returned verbatim to a client.
"""
from collections import deque
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

import provisioner
from ops import apk_repository, app_installer, events, inventory, proxy_store, resources
from ops.device_ids import canonical_device
from ops.secureio import (atomic_json, read_private_json, require_private_directory,
                          require_private_file, require_trusted_release_file)
from .components import Components, ComponentError
from .authstate import auth_revision


ROOT = Path(__file__).resolve().parents[2]
CATALOG = Path('/etc/android-farm/apps.json')
IDENTIFIER = re.compile(r'[a-z][a-z0-9-]{2,62}\Z')
LIFECYCLE = frozenset({'up', 'down', 'restart', 'check', 'check-ip', 'backup', 'release'})
PROXY_ACTIONS = frozenset({'proxy-test', 'proxy-enable', 'proxy-disable'})
ROLES = ('proxy', 'android', 'screen')
HOST_FAILURES = (
    ('calculated concurrent capacity reached', 'concurrent device capacity reached; stop a device before retrying'),
    ('insufficient available RAM', 'available RAM is below the reserved headroom; stop a device or retry later'),
    ('storage below', 'host storage is below the required free-space reserve'),
    ('host load too high', 'host load is too high; retry after existing work completes'),
    ('no safe capacity for another device', 'persistent storage has no safe capacity for another device'),
    ('device on manual safety hold', 'device is on a safety hold; an operator must review and release it before starting'),
    ('proxy enable failed health validation', 'proxy health validation failed; proxy remains disabled'),
    ('egress mismatch', 'egress IP verification failed; the device was stopped or placed on a safety hold'),
    ('does not match expected pinned IP', 'proxy egress differs from its configured fixed address'),
    ('managed proxy is disabled', 'the allocated proxy is disabled or no longer assigned to this device'),
    ('APK content hash mismatch', 'APK content differs from the approved catalog hash; review the artifact on the host'),
    ('APK signer is not allowed', 'APK signing certificate is not approved by the independent trust policy'),
    ('APK incompatible', 'the approved APK is incompatible with this Android SDK or CPU architecture'),
    ('Android boot timed out', 'Android did not finish booting before the deadline; inspect the device state'),
    ('device absent from Coolify Compose catalog', 'device capacity catalog needs regeneration through the installer'),
    ('resume the incomplete device', 'finish the existing incomplete device request before adding another'),
    ('resume or quarantine the incomplete device', 'finish or review the existing incomplete device before adding another'),
)


class OperationError(RuntimeError):
    """A controlled, secret-free explanation that may be shown to an operator."""


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional):
        raise ValueError('unsupported request fields')
    if set(required) - set(value):
        raise ValueError('required request fields are missing')


def _text(value, field, maximum=80):
    if (not isinstance(value, str) or not value or len(value) > maximum or
            any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f'invalid {field}')
    return value


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('invalid identifier')
    return value


def _device(value):
    if not isinstance(value, str) or canonical_device(value) != value:
        raise ValueError('device must be a canonical numNN identifier')
    return value


def _running_state(item):
    if not item:
        return 'missing'
    state = item.get('State') or {}
    if state.get('Restarting'):
        return 'restarting'
    if not state.get('Running'):
        return 'stopped'
    if state.get('Paused'):
        return 'paused'
    health = (state.get('Health') or {}).get('Status')
    return health if health in {'healthy', 'unhealthy', 'starting'} else 'running'


def bounded_process(argv, *, cwd, timeout=3600):
    """Drain process output while retaining at most 64 KiB; kill its tree on timeout."""
    chunks = deque(maxlen=16)
    command = argv
    if sys.platform == 'linux':
        command = [sys.executable, str(ROOT / 'services/api/runner.py'),
                   '--parent-pid', str(os.getpid()), '--', *argv]
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               start_new_session=(os.name == 'posix'))

    def drain():
        while True:
            block = process.stdout.read(4096)
            if not block:
                break
            chunks.append(block)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=10)
        raise OperationError('operation timed out; refresh device state before retrying') from None
    finally:
        thread.join(timeout=10)
        process.stdout.close()
    return subprocess.CompletedProcess(argv, process.returncode,
                                       b''.join(chunks).decode('utf-8', errors='replace'), '')


class Operations:
    def __init__(self, config_path=Path('/etc/android-farm/provisioner.json'), *,
                 catalog_path=CATALOG, runner=None, auth_file=Path('/data/coolify/proxy/dynamic/farm-users.htpasswd'),
                 credential_manager=None, components=None):
        self.config_path = Path(config_path)
        self.catalog_path = Path(catalog_path)
        self.runner = runner or bounded_process
        self.auth_file = Path(auth_file)
        self.credential_manager = credential_manager
        self.components = components or Components()

    def _credentials(self, config):
        if self.credential_manager is not None:
            return self.credential_manager
        from .credentials import CredentialManager
        return CredentialManager(self.auth_file, config_dir=self.config_path.parent,
                                 state_dir=config.state_dir)

    def _config(self):
        return provisioner.load_config(self.config_path)

    @staticmethod
    def _store(config):
        return proxy_store.ProxyStore(config.proxy_registry, config.proxy_store_dir)

    def _catalog(self):
        """Load trusted metadata only; file/signature verification happens at install."""
        if not self.catalog_path.exists() and not self.catalog_path.is_symlink():
            return {}
        require_trusted_release_file(self.catalog_path, 'application catalog')
        value = read_private_json(self.catalog_path, 'application catalog')
        # One validator serves the console, the CLI importer and provisioning.
        return apk_repository.validate_catalog(value)

    def stage_upload(self, stream, length):
        """Persist an authenticated APK upload privately; import happens in the queue."""
        config = self._config()
        token = apk_repository.stage_upload(stream, length, config.state_dir / 'web-uploads')
        return {'upload': token}

    def _staged_upload(self, config, token):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
            raise ValueError('invalid upload reference')
        directory = require_private_directory(config.state_dir / 'web-uploads', 'upload staging directory')
        return require_private_file(directory / f'{token}.apk', 'staged upload')

    def discard_upload(self, token):
        """Remove a staged upload whose import request could not be queued."""
        try:
            self._staged_upload(self._config(), token).unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError):
            pass

    @staticmethod
    def _artifact_available(app):
        try:
            path = require_trusted_release_file(Path(app['apk_path']), 'APK artifact')
            require_private_file(path, 'APK artifact')
            return 0 < path.stat().st_size <= 500 * 1024 ** 2
        except (OSError, RuntimeError):
            return False

    def validate_job(self, payload, *, trusted=False):
        _fields(payload, {'action'}, {'device', 'params', 'auth_revision'} if trusted else {'device', 'params'})
        action = payload['action']
        if not isinstance(action, str) or action not in LIFECYCLE | PROXY_ACTIONS | {
                'provision', 'proxy-add', 'credential-rotate', 'proxy-credentials', 'core-activate',
                'artifact-import', 'artifact-remove'}:
            raise ValueError('unsupported action')
        params = payload.get('params', {})
        if not isinstance(params, dict):
            raise ValueError('params must be an object')
        config = self._config()
        if action in LIFECYCLE:
            if action == 'release':
                _fields(params, {'review_completed'})
                if params['review_completed'] is not True:
                    raise ValueError('explicit confirmation of the completed operator review is required')
            else:
                _fields(params, set())
            device = _device(payload.get('device'))
            if not inventory.find(inventory.load(config.state_dir / 'inventory.json'), device):
                raise ValueError('device is not allocated in the managed inventory')
            return {'action': action, 'device': device, 'params': dict(params)}
        if 'device' in payload:
            raise ValueError('this action does not accept a device field')
        if action == 'core-activate':
            _fields(params, set())
        elif action == 'artifact-import':
            # The browser never names a path: it references its own staged upload.
            _fields(params, {'upload'}, {'label', 'id', 'permissions', 'activity', 'filename', 'allow_signer_change'})
            self._staged_upload(config, params['upload'])
            if 'label' in params:
                _text(params['label'], 'application label')
            if 'id' in params:
                _identifier(params['id'])
            if 'filename' in params:
                _text(params['filename'], 'source filename', 160)
            if 'activity' in params and (not isinstance(params['activity'], str) or
                                         not app_installer.ACTIVITY_RE.fullmatch(params['activity'])):
                raise ValueError('invalid Android activity')
            permissions = params.get('permissions', [])
            if (not isinstance(permissions, list) or len(permissions) > 3 or len(set(permissions)) != len(permissions)
                    or any(not isinstance(p, str) or p not in app_installer.ALLOWED_GRANTS for p in permissions)):
                raise ValueError('unsupported Android runtime permissions')
            if params.get('allow_signer_change', False) is not False and params.get('allow_signer_change') is not True:
                raise ValueError('allow_signer_change must be a boolean')
        elif action == 'artifact-remove':
            _fields(params, {'id'})
            if _identifier(params['id']) not in self._catalog():
                raise KeyError('application does not exist')
        elif action == 'credential-rotate':
            fields = {'target', 'username', 'password'}
            _fields(params, fields if trusted else fields | {'current_password'},
                    {'current_password'} if trusted else set())
            if params['target'] not in ('web', 'grafana', 'platform'):
                raise ValueError('مقصد تغییر رمز معتبر نیست.')
            if not isinstance(params['username'], str) or not re.fullmatch(r'[A-Za-z0-9._][A-Za-z0-9._-]{0,63}', params['username']):
                raise ValueError('نام کاربری باید ۱ تا ۶۴ کاراکتر مجاز باشد.')
            _text(params['password'], 'password', 72)
            if not 12 <= len(params['password'].encode('utf-8')) <= 72:
                raise ValueError('رمز جدید باید بین ۱۲ و ۷۲ بایت UTF-8 باشد.')
            if params['password'] != params['password'].strip():
                raise ValueError('ابتدا یا انتهای رمز نباید فاصله داشته باشد.')
            if 'current_password' in params:
                _text(params['current_password'], 'current password', 4096)
        elif action == 'proxy-credentials':
            fields = {'id', 'password'}
            _fields(params, fields if trusted else fields | {'current_password'},
                    {'current_password'} if trusted else set())
            self._store(config).show(_identifier(params['id']))
            _text(params['password'], 'proxy password', 4096)
            if params['password'] != params['password'].strip():
                raise ValueError('ابتدا یا انتهای رمز نباید فاصله داشته باشد.')
            if 'current_password' in params:
                _text(params['current_password'], 'current password', 4096)
        elif action == 'provision':
            _fields(params, {'phone', 'owner_authorized', 'artifact_id'}, {'proxy_id', 'egress'})
            if (not isinstance(params['phone'], str) or
                    not re.fullmatch(r'\+[1-9][0-9]{9,14}', params['phone']) or
                    params['owner_authorized'] is not True):
                raise ValueError('valid E.164 phone and explicit owner authorization are required')
            egress = params.get('egress', 'proxy')
            if egress not in ('proxy', 'direct'):
                raise ValueError('egress must be proxy or direct')
            app = self._catalog().get(_identifier(params['artifact_id']))
            if app is None or not self._artifact_available(app):
                raise ValueError('approved APK is unavailable; configure the private application catalog')
            if not config.apk_trust_file:
                raise ValueError('APK signer trust policy is not configured')
            if egress == 'direct':
                # Direct host egress never carries a proxy reference.
                if params.get('proxy_id') is not None:
                    raise ValueError('direct egress does not accept a proxy_id')
                params = {key: value for key, value in params.items() if key != 'proxy_id'}
            else:
                if 'proxy_id' not in params:
                    raise ValueError('proxy_id is required unless egress is direct')
                metadata = self._store(config).show(_identifier(params['proxy_id']))
                if metadata['state'] != 'enabled':
                    raise ValueError('proxy must be enabled before provisioning')
            params = dict(params, egress=egress)
        elif action == 'proxy-add':
            _fields(params, {'id', 'label', 'type', 'server', 'server_port', 'username',
                             'password', 'expected_egress_ip'})
            _identifier(params['id'])
            _text(params['label'], 'proxy label')
            if not isinstance(params['type'], str):
                raise ValueError('invalid proxy type')
            proxy_store._kind(params['type'])
            proxy_store._public_ipv4(params['server'], 'server')
            proxy_store._port(params['server_port'])
            proxy_store._public_ipv4(params['expected_egress_ip'], 'expected_egress_ip')
            _text(params['username'], 'username', 512)
            _text(params['password'], 'password', 4096)
        else:
            _fields(params, {'id'})
            self._store(config).show(_identifier(params['id']))
        return {'action': action, 'params': dict(params)}

    def _command(self, command, *arguments):
        argv = [sys.executable, str(ROOT / 'provisioner.py'), '--config',
                str(self.config_path), command, *arguments]
        result = self.runner(argv, cwd=str(ROOT), timeout=3600)
        if result.returncode:
            # Docker/ADB errors may contain credentials or environment values.
            # Never copy their raw output into the job store or HTTP response.
            output = result.stdout or ''
            for marker, message in HOST_FAILURES:
                if marker in output:
                    raise OperationError(message)
            raise OperationError('host operation failed; check device-provisioner on the host for details')
        return result.stdout or ''

    def execute(self, job):
        try:
            return self._execute(job)
        except OperationError:
            raise
        except (ValueError, OSError, RuntimeError, KeyError, subprocess.SubprocessError):
            raise OperationError('operation could not complete; refresh state and check the host configuration and proxy health') from None

    def _execute(self, job):
        generation = job.get('auth_revision')
        # Current-password reauthentication happened in the HTTP boundary and
        # was discarded before queue persistence. Revalidate every other field.
        job = self.validate_job(job, trusted=True)
        action, params = job['action'], job['params']
        config = self._config()
        if action in {'credential-rotate', 'proxy-credentials'} and generation != auth_revision(self.auth_file):
            raise OperationError('اطلاعات ورود فارم پس از ثبت درخواست تغییر کرده است؛ با حساب جدید دوباره درخواست دهید.')
        if action == 'core-activate':
            with provisioner.host_lifecycle_lock():
                try:
                    return dict(self.components.activate(), action=action)
                except ComponentError as exc:
                    raise OperationError(str(exc)) from None
        if action == 'credential-rotate':
            from .credentials import CredentialsError
            try:
                return dict(self._credentials(config).rotate(params['target'], params['username'], params['password']),
                            action=action)
            except CredentialsError as exc:
                raise OperationError(str(exc)) from None
        if action == 'artifact-import':
            staged = self._staged_upload(config, params['upload'])
            try:
                with provisioner.host_lifecycle_lock():
                    summary = apk_repository.import_apk(
                        staged, catalog_path=self.catalog_path, trust_path=config.apk_trust_file,
                        label=params.get('label'), app_id=params.get('id'),
                        permissions=params.get('permissions', ()), activity=params.get('activity'),
                        source_filename=params.get('filename'),
                        allow_signer_change=params.get('allow_signer_change', False))
            except apk_repository.RepositoryError as exc:
                raise OperationError(str(exc)) from None
            except subprocess.CalledProcessError:
                raise OperationError('the APK could not be inspected; verify that the file is a valid signed Android package') from None
            finally:
                staged.unlink(missing_ok=True)
            summary.pop('path', None)
            events.note('artifact-imported', None, f"{summary['package']} {summary.get('version_name') or ''}".strip())
            return {'action': action, 'completed': True, 'artifact': summary}
        if action == 'artifact-remove':
            with provisioner.host_lifecycle_lock():
                result = apk_repository.remove_app(params['id'], catalog_path=self.catalog_path)
            events.note('artifact-removed', None, params['id'])
            return {'action': action, 'completed': True, 'artifact': result}
        if action == 'proxy-credentials':
            # Use the existing guarded rotation with stop, pinned-IP validation,
            # two-copy synchronization and rollback. Never change session identity.
            directory = require_private_directory(config.state_dir / 'web-requests',
                                                   'web request directory', create=True)
            path = directory / (uuid.uuid4().hex + '.password')
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(params['password'] + '\n')
                result = provisioner.rotate_managed_proxy_password(
                    self._store(config), params['id'], path, config)
                return {'action': action, 'completed': True, 'proxy': result}
            finally:
                path.unlink(missing_ok=True)
        if action in LIFECYCLE:
            device = job['device']
            flags = ('--json',) if action == 'check-ip' else ('--review-completed',) if action == 'release' else ()
            output = self._command(action, '--id', device, *flags)
            result = {'action': action, 'device': device, 'completed': True}
            if action in ('up', 'restart'):
                result['screen_path'] = f'/d/{device}/'
            if action == 'check-ip':
                try:
                    evidence = json.loads(output)
                    for key in ('proxy_namespace', 'android_shell', 'expected'):
                        result[key] = str(ipaddress.IPv4Address(evidence[key]))
                    result['matches'] = evidence.get('matches') is True
                except (ValueError, KeyError, TypeError):
                    raise OperationError('host returned invalid IP check evidence') from None
            return result
        if action == 'provision':
            app = self._catalog()[params['artifact_id']]
            request = {key: value for key, value in app.items() if key.startswith('apk_')}
            request.update(phone=params['phone'], owner_authorized=True, egress=params['egress'])
            if params['egress'] != 'direct':
                request['proxy_id'] = params['proxy_id']
            directory = require_private_directory(config.state_dir / 'web-requests',
                                                   'web request directory', create=True)
            path = directory / (uuid.uuid4().hex + '.json')
            atomic_json(path, request)
            try:
                output = self._command('up', '--request', str(path))
            finally:
                path.unlink(missing_ok=True)
            matches = re.findall(r'^(num\d{2,}):', output, re.MULTILINE)
            if not matches:
                raise OperationError('host returned no provisioned device identifier; refresh inventory')
            device = _device(matches[-1])
            return {'action': action, 'device': device, 'completed': True,
                    'screen_path': f'/d/{device}/'}
        store = self._store(config)
        if action == 'proxy-add':
            with provisioner.host_lifecycle_lock():
                record = store.add(params['id'], label=params['label'], proxy_type=params['type'],
                                   server=params['server'], server_port=params['server_port'],
                                   username=params['username'], password=params['password'],
                                   expected_egress_ip=params['expected_egress_ip'])
            return {'action': action, 'completed': True, 'proxy': record}
        with provisioner.host_lifecycle_lock():
            if action == 'proxy-test':
                result = store.check_health(params['id'])
            elif action == 'proxy-disable':
                assigned = store.show(params['id']).get('assigned_device')
                if assigned:
                    self._command('hold', '--id', _device(assigned), '--reason', 'maintenance')
                result = store.disable(params['id'])
                result['assigned_device_stopped'] = bool(assigned)
                result['safety_hold_created'] = bool(assigned)
            else:
                store.enable(params['id'])
                try:
                    result = store.check_health(params['id'])
                except Exception:
                    store.disable(params['id'])
                    raise OperationError('proxy health validation failed; proxy remains disabled') from None
        return {'action': action, 'completed': True, 'proxy': result}

    @staticmethod
    def _backups(config):
        root = config.backup_dir
        if not root.exists():
            return []
        require_private_directory(root, 'backup directory')
        entries = []
        for folder in root.iterdir():
            try:
                device = _device(folder.name)
            except ValueError:
                continue
            if folder.is_symlink() or not folder.is_dir():
                continue
            require_private_directory(folder, 'device backup directory')
            for path in folder.glob('*.tar'):
                if path.is_symlink() or not path.is_file():
                    continue
                metadata = require_private_file(path, 'device backup').stat()
                entries.append({'id': f'{device}/{path.name}', 'device': device,
                                'created_at': int(metadata.st_mtime), 'size_bytes': metadata.st_size})
        return sorted(entries, key=lambda row: row['created_at'], reverse=True)

    def snapshot(self):
        result = {'collected_at': int(time.time()), 'resources': None, 'devices': [], 'proxies': [],
                  'backups': [], 'artifacts': [], 'errors': [], 'settings': {}}
        result['components'] = self.components.snapshot()

        def failure(component, message):
            result['errors'].append({'component': component, 'message': message})

        try:
            result['events'] = events.recent(100)
        except (OSError, RuntimeError, ValueError):
            result['events'] = []
            failure('events', 'رویدادهای دستگاه‌ها خوانده نشد؛ مالکیت فایل رویداد را بررسی کنید.')

        try:
            config = self._config()
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            failure('configuration', 'private host configuration is missing or invalid')
            return result
        result['settings'] = {'console_url': config.console_url, 'access_mode': config.access_mode}
        result['settings']['central_activation_available'] = True
        try:
            result['settings']['security'] = dict(self._credentials(config).summary(), proxy_credentials_available=True)
        except (OSError, RuntimeError, ValueError):
            result['settings']['security'] = {'web_username': None, 'grafana_username': None,
                                              'credential_rotation_available': False,
                                              'proxy_credentials_available': True}
            failure('credentials', 'تنظیمات خصوصی حساب مدیریت در دسترس نیست.')
        try:
            result['resources'] = resources.probe()
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError):
            failure('resources', 'host capacity probe is unavailable')
        try:
            result['proxies'] = self._store(config).list()
        except (OSError, RuntimeError, ValueError, KeyError):
            failure('proxies', 'private proxy registry is unavailable or invalid')
        try:
            result['backups'] = self._backups(config)
        except (OSError, RuntimeError, ValueError):
            failure('backups', 'backup catalog could not be read safely')
        try:
            catalog = self._catalog()
            result['artifacts'] = [{'id': app['id'], 'label': app['label'],
                                    'package': app['apk_package'], 'available': self._artifact_available(app),
                                    'version': app.get('apk_version_name'),
                                    'signers': list(app.get('apk_signers', [])),
                                    'imported_at': app.get('imported_at')}
                                   for app in catalog.values()]
            # A new installation deliberately has no approved applications. This
            # requires setup before provisioning, but is not an API/service fault.
            result['settings']['application_catalog'] = {
                'state': ('setup_required' if not catalog else
                          'ready' if any(app['available'] for app in result['artifacts']) else 'files_required')}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            result['settings']['application_catalog'] = {'state': 'invalid'}
            failure('artifacts', 'فهرست خصوصی برنامه‌ها نامعتبر یا دسترسی آن ناامن است؛ بخش ۹ راهنمای واحد را بررسی کنید.')
        try:
            records = inventory.records(inventory.load(config.state_dir / 'inventory.json'))
        except (OSError, RuntimeError, ValueError, KeyError):
            failure('inventory', 'private device inventory is unavailable or invalid')
            return result
        try:
            hold_file = config.state_dir / 'holds.json'
            holds = read_private_json(hold_file, 'safety holds') if hold_file.exists() else {}
            if not isinstance(holds, dict):
                raise ValueError('invalid holds')
        except (OSError, RuntimeError, ValueError):
            holds = {record['id']: {'reason': 'unavailable'} for record in records}
            failure('holds', 'safety hold state is unavailable; device readiness cannot be established')
        inspections = {}
        docker_available = True
        try:
            inspections = provisioner.bulk_managed_inspections([record['id'] for record in records])
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
            docker_available = False
            failure('docker', 'managed container state is unavailable')
        running = []
        proxies = {item['id']: item for item in result['proxies']}
        for record in records:
            device = record['id']
            states = {role: _running_state(inspections.get(f'{role}-{device}'))
                      if docker_available else 'unknown' for role in ROLES}
            proxy = inspections.get(f'proxy-{device}') or {}
            ports = (proxy.get('NetworkSettings') or {}).get('Ports') or {}
            bindings = ports.get('5555/tcp') or []
            port = next((str(b.get('HostPort')) for b in bindings
                         if str(b.get('HostPort', '')).isdigit() and b.get('HostIp') == '127.0.0.1'), None)
            metadata = proxies.get(record.get('proxy_id')) or {}
            hold = holds.get(device)
            has_hold = device in holds
            # Return only the documented reason/timestamp, not arbitrary private JSON.
            safe_hold = None
            if has_hold:
                reasons = {'account-restriction', 'ip-change', 'ownership-review', 'maintenance', 'unavailable'}
                reason = hold.get('reason') if isinstance(hold, dict) else None
                held_at = hold.get('at') if isinstance(hold, dict) else None
                safe_hold = {'reason': reason if isinstance(reason, str) and reason in reasons else 'unknown',
                             'at': held_at if isinstance(held_at, (int, float)) else None}
            active = bool((inspections.get(f'android-{device}') or {}).get('State', {}).get('Running'))
            row = {'id': device, 'phase': record.get('phase', 'unknown'), 'hold': safe_hold,
                   'containers': states, 'adb': f'127.0.0.1:{port}' if port else None,
                   'screen': provisioner.web_url(config, device), 'screen_path': f'/d/{device}/',
                   'proxy_id': record.get('proxy_id'),
                   'egress': 'direct' if record.get('egress') == 'direct' else 'proxy',
                   'proxy': f"{metadata['type']}://{metadata['server']}:{metadata['server_port']}" if metadata else None,
                   'expected_egress_ip': record.get('expected_egress_ip'),
                   'phone': record.get('phone_masked'), 'cpu': None, 'memory': None,
                   'running': active,
                   'screen_ready': not has_hold and all(state in {'running', 'healthy'} for state in states.values())}
            result['devices'].append(row)
            if active:
                running.append(f'android-{device}')
        if running:
            try:
                values = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *running],
                                        capture_output=True, text=True, check=True, timeout=45)
                stats = {item['Name']: item for item in
                         (json.loads(line) for line in values.stdout.splitlines() if line.strip())}
                for row in result['devices']:
                    item = stats.get(f'android-{row["id"]}')
                    if item:
                        row['cpu'], row['memory'] = item.get('CPUPerc'), item.get('MemUsage')
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                failure('metrics', 'live container resource measurements are unavailable')
        return result
