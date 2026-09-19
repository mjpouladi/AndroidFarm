"""Coordinated private credential changes for the farm and existing Grafana DB.

The HTTP adapter must reauthenticate the operator before queuing a change. This
module is also usable by the root installer without an API process. Passwords
travel through stdin, private files or HTTP headers/bodies, never command argv.
"""
import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from ops.secureio import read_private_json, require_private_directory


USER_RE = re.compile(r'[A-Za-z0-9._][A-Za-z0-9._-]{0,63}\Z')
REALM_RE = re.compile(r'^        realm: [\'"]Android Farm [0-9a-f]{64}[\'"]$')
DEFAULT_MIDDLEWARE = b'''http:
  middlewares:
    farm-auth:
      basicAuth:
        usersFile: /traefik/dynamic/farm-users.htpasswd
        removeHeader: true
    farm-console-auth:
      basicAuth:
        usersFile: /traefik/dynamic/farm-users.htpasswd
        removeHeader: false
'''

# These are the only root scripts credential rotation may restart. Keep them
# aligned with the reviewed Compose init jobs, including the password-only
# Grafana job installed by earlier releases during an upgrade.
GATEWAY_INIT_SCRIPT = (
    'umask 077; '
    'cp /run/secrets/farm-http-auth /private/farm-users.htpasswd.next; '
    'chown 101:101 /private/farm-users.htpasswd.next; '
    'chmod 0400 /private/farm-users.htpasswd.next; '
    'mv -f /private/farm-users.htpasswd.next /private/farm-users.htpasswd')
GRAFANA_PASSWORD_SCRIPT = (
    'umask 077; '
    'cp /run/secrets/grafana-admin-password /private/admin-password.next; '
    'chown 472:0 /private/admin-password.next; '
    'chmod 0400 /private/admin-password.next; '
    'mv -f /private/admin-password.next /private/admin-password; ')
GRAFANA_LEGACY_INIT_SCRIPT = GRAFANA_PASSWORD_SCRIPT + 'chown -R 472:0 /grafana-data'
GRAFANA_INIT_SCRIPT = GRAFANA_PASSWORD_SCRIPT + (
    'cp /run/secrets/grafana-admin-user /private/admin-user.next; '
    'chown 472:0 /private/admin-user.next; '
    'chmod 0400 /private/admin-user.next; '
    'mv -f /private/admin-user.next /private/admin-user; '
    'chown -R 472:0 /grafana-data')
COMPOSE_SERVICES = {
    'android-farm-gateway': 'gateway',
    'android-farm-grafana': 'grafana',
    'android-farm-gateway-secret-init': 'gateway-secret-init',
    'android-farm-grafana-secret-init': 'grafana-secret-init',
}
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in
                         ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
FAILURE_DETAILS = {
    'init-policy': 'تنظیم کانتینر همگام‌سازی با سیاست مدیریت‌شده سازگار نیست.',
    'tool-failed': 'ابزار محلی مدیریت رمز موفق نشد.',
    'tool-unavailable': 'ابزار محلی اجرا نشد یا مهلت اجرای آن پایان یافت.',
    'tool-output': 'خروجی ابزار محلی ساخت bcrypt یک رکورد معتبر نبود.',
    'grafana-auth': 'Grafana اطلاعات ورود ذخیره‌شده را نپذیرفت.',
    'grafana-forbidden': 'Grafana مجوز تغییر حساب را نداد.',
    'grafana-http': 'Grafana پاسخ HTTP موفق نداد.',
    'grafana-response': 'پاسخ Grafana قابل تأیید نیست.',
    'grafana-unreachable': 'اتصال مستقیم به Grafana برقرار نشد.',
    'file-missing': 'یکی از فایل‌ها یا پوشه‌های لازم وجود ندارد.',
    'file-permission': 'خواندن یا نوشتن فایل خصوصی مجاز نبود.',
    'credential-operation': 'تنظیمات سرویس‌ها و فایل‌های خصوصی باید بررسی شوند.',
}


class CredentialsError(RuntimeError):
    """Controlled secret-free failure suitable for the authenticated UI."""

    def __init__(self, message, *, code='credential-operation'):
        super().__init__(message)
        self.code = code if code in FAILURE_DETAILS else 'credential-operation'


def _failure_detail(error, stage):
    # Never include raw subprocess output, HTTP responses or exception text.
    code = error.code if isinstance(error, CredentialsError) else (
        'file-missing' if isinstance(error, FileNotFoundError) else
        'file-permission' if isinstance(error, PermissionError) else 'credential-operation')
    return f'[{stage}/{code}] {FAILURE_DETAILS[code]}'


def validate_credentials(username, password):
    if not isinstance(username, str) or not USER_RE.fullmatch(username):
        raise CredentialsError('نام کاربری باید ۱ تا ۶۴ نویسهٔ مجاز داشته باشد و با خط تیره شروع نشود.')
    try:
        length = len(password.encode('utf-8')) if isinstance(password, str) else 0
    except UnicodeError:
        length = 0
    if (not isinstance(password, str) or not 12 <= length <= 72 or password != password.strip() or
            any(ord(character) < 32 or ord(character) == 127 for character in password)):
        raise CredentialsError('رمز باید ۱۲ تا ۷۲ بایت UTF-8 و بدون نویسهٔ کنترلی باشد.')


def _middleware_lines(content):
    lines = content.decode('utf-8').splitlines()
    return [line.rstrip() for line in lines if line.strip() and
            not line.lstrip().startswith('#') and not REALM_RE.fullmatch(line.rstrip())]


def middleware_matches(content, reference):
    """Ignore only our bounded revision realm and comments, never other edits."""
    try:
        return _middleware_lines(content) == _middleware_lines(reference)
    except (UnicodeError, AttributeError):
        return False


def render_middleware(content, revision):
    if not re.fullmatch(r'[0-9a-f]{64}', revision) or not middleware_matches(content, DEFAULT_MIDDLEWARE):
        raise CredentialsError('میان‌افزار احراز هویت با نسخهٔ مدیریت‌شده سازگار نیست.')
    lines = []
    for line in content.decode('utf-8').splitlines():
        if REALM_RE.fullmatch(line.rstrip()):
            continue
        lines.append(line)
        if line.strip() == 'usersFile: /traefik/dynamic/farm-users.htpasswd':
            # Traefik reads usersFile when creating the middleware. Changing
            # the actual realm field forces rebuilding it without proxy restart.
            lines.append(f'        realm: "Android Farm {revision}"')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def _capabilities_match(values, expected):
    """Compare Docker's canonical CAP_* names and legacy Compose spellings.

    Moby normalizes capability names before container creation. Only aliases
    for the exact reviewed set are accepted; normalization must not make an
    extra capability, a missing drop, or an invalid inspect value acceptable.
    """
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        return False
    aliases = {name: name for name in expected}
    aliases.update({'CAP_' + name: name for name in expected if name != 'ALL'})
    normalized = [aliases.get(value.upper()) for value in values]
    return None not in normalized and set(normalized) == expected


def _safe_path(path, *, dynamic=False, public=False, missing=False):
    path = Path(path)
    if not path.is_absolute():
        raise CredentialsError('مسیر مدیریت‌شده باید مطلق باشد.')
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        owners = {0, 9999} if dynamic else {0}
        if parent.is_symlink() or not parent.is_dir() or (os.name == 'posix' and
                (info.st_uid not in owners or info.st_mode & 0o022)):
            raise CredentialsError('مالکیت یا مجوز پوشهٔ اطلاعات ورود امن نیست.')
    if not path.exists() and not path.is_symlink() and missing:
        return path
    info = path.lstat()
    if (path.is_symlink() or not path.is_file() or info.st_size > 1024 * 1024 or
            (os.name == 'posix' and (info.st_uid not in ({0, 9999} if dynamic else {0}) or
                                    info.st_mode & (0o022 if public else 0o077)))):
        raise CredentialsError('فایل اطلاعات ورود مالکیت یا مجوز امن ندارد.')
    return path


def _write(path, content, *, dynamic=False, public=False):
    path = _safe_path(path, dynamic=dynamic, public=public, missing=True)
    descriptor, name = tempfile.mkstemp(prefix='.credential-', dir=path.parent)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(descriptor, 0o644 if public else 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


@contextmanager
def _lock(path):
    require_private_directory(path.parent, 'credential state directory')
    _safe_path(path, missing=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == 'posix':
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(method, url, username, password, payload=None):
    auth = base64.b64encode(f'{username}:{password}'.encode()).decode('ascii')
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={'Authorization': 'Basic ' + auth,
                                              'Content-Type': 'application/json', 'Accept': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            content = response.read(65537)
            if len(content) > 65536:
                raise CredentialsError('پاسخ سرویس مانیتورینگ بیش از حد بزرگ بود.', code='grafana-response')
            result = json.loads(content)
            if not isinstance(result, dict):
                raise CredentialsError('پاسخ سرویس مانیتورینگ معتبر نیست.', code='grafana-response')
            return result
    except urllib.error.HTTPError as error:
        code = 'grafana-auth' if error.code == 401 else 'grafana-forbidden' if error.code == 403 else 'grafana-http'
        error.close()
        raise CredentialsError(FAILURE_DETAILS[code], code=code) from None
    except ValueError:
        raise CredentialsError(FAILURE_DETAILS['grafana-response'], code='grafana-response') from None
    except (OSError, urllib.error.URLError):
        raise CredentialsError(FAILURE_DETAILS['grafana-unreachable'], code='grafana-unreachable') from None


class CredentialManager:
    def __init__(self, auth_file, config_dir=Path('/etc/android-farm'),
                 state_dir=Path('/var/lib/android-farm'), *, runner=subprocess.run, requester=_http):
        self.auth_file = Path(auth_file)
        self.config_dir, self.state_dir = Path(config_dir), Path(state_dir)
        self.runner, self.requester = runner, requester
        self.web_password = self.config_dir / 'web-login-password'
        self.web_user = self.config_dir / 'web-login-user'
        self.grafana_password = self.config_dir / 'monitoring/grafana-admin-password'
        self.grafana_user = self.config_dir / 'monitoring/grafana-admin-user'

    def _run(self, argv, *, input=None, timeout=30):
        try:
            result = self.runner(argv, input=input, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            raise CredentialsError('اجرای ابزار مدیریت اطلاعات ورود ناموفق بود.', code='tool-unavailable') from None
        if result.returncode:
            raise CredentialsError('ابزار مدیریت اطلاعات ورود عملیات را تأیید نکرد.', code='tool-failed')
        return result.stdout or ''

    def _user(self):
        if self.auth_file.name != 'farm-users.htpasswd':
            raise CredentialsError('فایل ورود با مسیر مدیریت‌شدهٔ فارم سازگار نیست.')
        data = _safe_path(self.auth_file, dynamic=True).read_bytes()
        lines = data.decode('utf-8').splitlines()
        if len(lines) != 1:
            raise CredentialsError('تغییر متمرکز اطلاعات ورود فقط برای حساب مدیریت تک‌کاربره فعال است.')
        user, separator, value = lines[0].partition(':')
        if not separator or not USER_RE.fullmatch(user) or not value.startswith(('$2a$', '$2b$', '$2y$')):
            raise CredentialsError('فایل bcrypt حساب مدیریت معتبر نیست.')
        return user, data

    @staticmethod
    def _password(path):
        text = _safe_path(path).read_text(encoding='utf-8')
        if text.endswith('\n'):
            text = text[:-1]
        if not text or len(text.encode()) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise CredentialsError('فایل خصوصی رمز معتبر نیست.')
        return text

    def _inspect(self, name, role):
        try:
            rows = json.loads(self._run(['docker', 'inspect', name]))
            item = rows[0]
            labels = item['Config'].get('Labels') or {}
            if (len(rows) != 1 or item.get('Name') != '/' + name or labels.get('farm.stack') != 'core' or
                    labels.get('farm.role') != role or
                    labels.get('com.docker.compose.project') != 'android-farm-core' or
                    labels.get('com.docker.compose.service') != COMPOSE_SERVICES.get(name)):
                raise ValueError()
            return item
        except (ValueError, IndexError, KeyError, TypeError):
            raise CredentialsError('هویت کانتینر مدیریت‌شده تأیید نشد.') from None

    def _init_plan(self, name, host_secret, service_name, role, target):
        init = self._inspect(name, 'secret-init')
        service = self._inspect(service_name, role)
        state = init.get('State') or {}
        host = init.get('HostConfig') or {}
        config = init['Config']
        command = config.get('Cmd')
        script = (command[2] if isinstance(command, list) and len(command) == 3 and
                  command[:2] == ['/bin/sh', '-ec'] and isinstance(command[2], str) else None)
        scripts = {GATEWAY_INIT_SCRIPT} if role == 'gateway' else {GRAFANA_INIT_SCRIPT, GRAFANA_LEGACY_INIT_SCRIPT}
        if (state.get('Running') or host.get('NetworkMode') != 'none' or
                host.get('ReadonlyRootfs') is not True or config.get('User') != '0:0' or
                config.get('Entrypoint') not in (None, []) or config.get('Image') != 'alpine:3.21' or
                script not in scripts or host.get('Privileged') is not False or
                not _capabilities_match(host.get('CapDrop'), {'ALL'}) or
                not _capabilities_match(host.get('CapAdd'), {'CHOWN', 'DAC_OVERRIDE', 'FOWNER'}) or
                host.get('SecurityOpt') not in (['no-new-privileges:true'], ['no-new-privileges=true'],
                                                 ['no-new-privileges']) or
                host.get('PidMode') not in (None, '') or host.get('IpcMode') not in (None, '', 'private') or
                host.get('Devices') or host.get('DeviceRequests')):
            raise CredentialsError('کانتینر همگام‌سازی رمز آمادهٔ اجرای محدود نیست.', code='init-policy')
        binds = [m for m in init.get('Mounts', []) if m.get('Destination') == target]
        private = [m for m in init.get('Mounts', []) if m.get('Destination') == '/private']
        mounted = [m for m in service.get('Mounts', []) if m.get('Destination') in
                   {'/run/gateway-private', '/run/grafana-private'}]
        if (len(binds) != 1 or binds[0].get('Type') != 'bind' or binds[0].get('RW') is not False or
                Path(binds[0].get('Source', '')) != host_secret or len(private) != 1 or len(mounted) != 1 or
                private[0].get('Type') != 'volume' or private[0].get('RW') is not True or
                mounted[0].get('Type') != 'volume' or mounted[0].get('RW') is not False or
                not private[0].get('Name') or private[0]['Name'] != mounted[0].get('Name')):
            raise CredentialsError('اتصال فایل خصوصی به volume سرویس با تنظیمات مدیریت‌شده یکسان نیست.')
        expected = {target, '/private'}
        if role == 'grafana':
            data = [m for m in init.get('Mounts', []) if m.get('Destination') == '/grafana-data']
            live_data = [m for m in service.get('Mounts', []) if m.get('Destination') == '/var/lib/grafana']
            if (len(data) != 1 or len(live_data) != 1 or data[0].get('Type') != 'volume' or
                    data[0].get('RW') is not True or live_data[0].get('Type') != 'volume' or
                    live_data[0].get('RW') is not True or not data[0].get('Name') or
                    data[0]['Name'] != live_data[0].get('Name')):
                raise CredentialsError('volume دادهٔ Grafana با سرویس مدیریت‌شده یکسان نیست.')
            expected.add('/grafana-data')
            if script == GRAFANA_INIT_SCRIPT:
                user_mount = [m for m in init.get('Mounts', []) if m.get('Destination') == '/run/secrets/grafana-admin-user']
                if (len(user_mount) != 1 or user_mount[0].get('Type') != 'bind' or
                        user_mount[0].get('RW') is not False or
                        Path(user_mount[0].get('Source', '')) != self.grafana_user):
                    raise CredentialsError('اتصال فایل خصوصی نام کاربری Grafana معتبر نیست.')
                expected.add('/run/secrets/grafana-admin-user')
        mounts = init.get('Mounts', [])
        if len(mounts) != len(expected) or {m.get('Destination') for m in mounts} != expected:
            raise CredentialsError('کانتینر همگام‌سازی رمز اتصال اضافی یا نامعتبر دارد.')
        return name

    def _refresh(self, plan):
        self._run(['docker', 'start', '--attach', plan], timeout=60)
        item = self._inspect(plan, 'secret-init')
        if item.get('State', {}).get('Running') or item.get('State', {}).get('ExitCode') != 0:
            raise CredentialsError('همگام‌سازی رمز در volume سرویس کامل نشد.')

    def summary(self):
        try:
            user, _ = self._user()
            grafana_user = self._password(self.grafana_user) if self.grafana_user.exists() else 'admin'
            if not USER_RE.fullmatch(grafana_user):
                raise CredentialsError('نام کاربری مانیتورینگ معتبر نیست.')
            return {'web_username': user, 'grafana_username': grafana_user,
                    'credential_rotation_available': True}
        except (OSError, ValueError, CredentialsError):
            return {'web_username': None, 'grafana_username': None, 'credential_rotation_available': False}

    def _environment_changes(self, updates):
        changes = []
        for name in ('compose.env', 'coolify.env'):
            path = self.config_dir / name
            if not path.exists():
                continue
            original = _safe_path(path).read_bytes()
            lines = original.decode('utf-8').splitlines()
            for key, value in updates.items():
                lines = [line for line in lines if not line.startswith(key + '=')]
                lines.append(f'{key}={value}')
            changes.append((path, original, ('\n'.join(lines) + '\n').encode(), False, False))
        return changes

    @staticmethod
    def _apply_files(changes, *, rollback=False):
        values = reversed(changes) if rollback else changes
        for path, before, after, dynamic, public in values:
            value = before if rollback else after
            if value is None:
                _safe_path(path, dynamic=dynamic, public=public, missing=True)
                path.unlink(missing_ok=True)
            else:
                _write(path, value, dynamic=dynamic, public=public)

    def _prepare_web(self, username, password):
        old_user, old_hash = self._user()
        middleware = self.auth_file.parent / 'farm-auth.yml'
        original = _safe_path(middleware, dynamic=True, public=True).read_bytes()
        if not middleware_matches(original, DEFAULT_MIDDLEWARE):
            raise CredentialsError('میان‌افزار سفارشی است؛ تغییر ورود باید پس از بررسی تنظیمات انجام شود.')
        # apache2-utils prints the record followed by an extra blank line
        # (``user:hash\n\n``). Keep exactly one record and one newline so the
        # single-user check in _user() still holds after this rotation.
        record = self._run(['htpasswd', '-niB', username], input=password + '\n').strip()
        if (not record.startswith(username + ':$2') or len(record.splitlines()) != 1 or
                any(character.isspace() for character in record)):
            raise CredentialsError('ساخت bcrypt جدید تأیید نشد.', code='tool-output')
        new_hash = (record + '\n').encode()
        revision = hashlib.sha256(new_hash).hexdigest()
        before_password = _safe_path(self.web_password).read_bytes() if self.web_password.exists() else None
        before_user = _safe_path(self.web_user).read_bytes() if self.web_user.exists() else None
        changes = [(self.auth_file, old_hash, new_hash, True, False),
                   (self.web_password, before_password, (password + '\n').encode(), False, False),
                   (self.web_user, before_user, (username + '\n').encode(), False, False),
                   (middleware, original, render_middleware(original, revision), True, True)]
        state_file = self.state_dir / 'quickstart.json'
        if state_file.exists():
            state = read_private_json(state_file, 'installer metadata')
            if not isinstance(state, dict):
                raise CredentialsError('وضعیت راه‌انداز معتبر نیست.')
            updated = dict(state, auth_user=username)
            changes.append((state_file, state_file.read_bytes(),
                            (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode(), False, False))
        plan = self._init_plan('android-farm-gateway-secret-init', self.auth_file,
                               'android-farm-gateway', 'gateway', '/run/secrets/farm-http-auth')
        return {'changes': changes, 'plan': plan, 'revision': revision}

    def _prepare_grafana(self, username, password):
        item = self._inspect('android-farm-grafana', 'grafana')
        if not item.get('State', {}).get('Running'):
            raise CredentialsError('Grafana باید برای تغییر رمز پایگاه داده روشن باشد.')
        env = dict(line.split('=', 1) for line in item['Config'].get('Env', []) if '=' in line)
        old_user = self._password(self.grafana_user) if self.grafana_user.exists() else env.get('GF_SECURITY_ADMIN_USER', 'admin')
        if not USER_RE.fullmatch(old_user):
            raise CredentialsError('نام کاربری فعلی Grafana معتبر نیست.')
        old_password = self._password(self.grafana_password)
        networks = (item.get('NetworkSettings') or {}).get('Networks') or {}
        addresses = []
        for details in networks.values():
            try:
                address = ipaddress.IPv4Address(details.get('IPAddress', ''))
                if any(address in network for network in PRIVATE_NETWORKS):
                    addresses.append(str(address))
            except (ValueError, AttributeError):
                pass
        if not addresses:
            raise CredentialsError('آدرس خصوصی کانتینر Grafana در شبکهٔ داکر پیدا نشد.')
        prefix = ''
        if env.get('GF_SERVER_SERVE_FROM_SUB_PATH', '').lower() == 'true':
            prefix = urlsplit(env.get('GF_SERVER_ROOT_URL', '')).path.rstrip('/')
            if not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', prefix):
                raise CredentialsError('مسیر داخلی Grafana معتبر نیست.')
        base = f'http://{sorted(addresses)[0]}:3000{prefix}'
        profile = self.requester('GET', base + '/api/user', old_user, old_password)
        if (not isinstance(profile.get('id'), int) or profile['id'] < 1 or
                profile.get('isGrafanaAdmin') is not True or profile.get('login') != old_user):
            raise CredentialsError('حساب ذخیره‌شده مدیر Grafana نیست؛ تغییر انجام نشد.')
        before_user = _safe_path(self.grafana_user).read_bytes() if self.grafana_user.exists() else None
        changes = [(self.grafana_password, _safe_path(self.grafana_password).read_bytes(),
                    (password + '\n').encode(), False, False),
                   (self.grafana_user, before_user, (username + '\n').encode(), False, False)]
        changes.extend(self._environment_changes({'GRAFANA_ADMIN_USER': username}))
        plan = self._init_plan('android-farm-grafana-secret-init', self.grafana_password,
                               'android-farm-grafana', 'grafana', '/run/secrets/grafana-admin-password')
        return {'base': base, 'profile': profile, 'old_user': old_user, 'old_password': old_password,
                'username': username, 'password': password, 'changes': changes, 'plan': plan}

    def _grafana_set(self, prepared, source_user, source_password, username, password):
        base, profile = prepared['base'], prepared['profile']
        if source_password != password:
            self.requester('PUT', base + '/api/user/password', source_user, source_password,
                           {'oldPassword': source_password, 'newPassword': password, 'confirmNew': password})
        if source_user != username:
            body = {key: profile[key] for key in ('email', 'name', 'theme') if isinstance(profile.get(key), str)}
            body['login'] = username
            self.requester('PUT', base + f'/api/users/{profile["id"]}', source_user, password, body)
        verified = self.requester('GET', base + '/api/user', username, password)
        if verified.get('id') != profile['id'] or verified.get('login') != username:
            raise CredentialsError('تأیید نهایی حساب Grafana ناموفق بود.')

    def _grafana_rollback(self, prepared):
        # A timed-out PUT may have committed. Discover which known credential
        # currently works before attempting rollback; never guess success.
        for user, password in ((prepared['username'], prepared['password']),
                               (prepared['old_user'], prepared['password']),
                               (prepared['old_user'], prepared['old_password'])):
            try:
                current = self.requester('GET', prepared['base'] + '/api/user', user, password)
                if current.get('id') == prepared['profile']['id']:
                    self._grafana_set(prepared, user, password, prepared['old_user'], prepared['old_password'])
                    self._apply_files(prepared['changes'], rollback=True)
                    self._refresh(prepared['plan'])
                    return
            except Exception:
                continue
        raise CredentialsError('بازگردانی حساب Grafana تأیید نشد.')

    def rotate(self, target, username, password):
        validate_credentials(username, password)
        if target not in {'web', 'grafana', 'platform'}:
            raise CredentialsError('بخش انتخاب‌شده برای تغییر اطلاعات ورود معتبر نیست.')
        require_private_directory(self.config_dir, 'credential config')
        require_private_directory(self.state_dir, 'credential state')
        web = grafana = None
        web_attempted = grafana_attempted = False
        stage = 'preflight'
        with _lock(self.state_dir / 'credential.lock'):
            try:
                # Validate all endpoints/files before changing either account.
                if target in {'web', 'platform'}:
                    stage = 'web-preflight'
                    web = self._prepare_web(username, password)
                if target in {'grafana', 'platform'}:
                    stage = 'grafana-preflight'
                    grafana = self._prepare_grafana(username, password)
                if grafana:
                    stage = 'grafana-account'
                    grafana_attempted = True
                    self._grafana_set(grafana, grafana['old_user'], grafana['old_password'], username, password)
                    stage = 'grafana-files'
                    self._apply_files(grafana['changes'])
                    stage = 'grafana-secret-sync'
                    self._refresh(grafana['plan'])
                if web:
                    # Grafana may have just updated these shared env files.
                    # Merge the auth revision against that current content so
                    # a platform change cannot restore the previous login.
                    stage = 'web-files'
                    web['changes'].extend(self._environment_changes({'FARM_HTTP_AUTH_REVISION': web['revision']}))
                    web_attempted = True
                    self._apply_files(web['changes'])
                    stage = 'web-secret-sync'
                    self._refresh(web['plan'])
            except Exception as error:
                detail = _failure_detail(error, stage)
                rollback_failed = False
                if web_attempted:
                    try:
                        self._apply_files(web['changes'], rollback=True)
                        self._refresh(web['plan'])
                    except Exception:
                        rollback_failed = True
                if grafana_attempted:
                    try:
                        self._grafana_rollback(grafana)
                    except Exception:
                        rollback_failed = True
                if rollback_failed:
                    recovery = {'target': target, 'username': username, 'password': password,
                                'created_at': int(time.time()), 'requires_review': True}
                    _write(self.config_dir / 'credential-recovery.json',
                           (json.dumps(recovery) + '\n').encode())
                    raise CredentialsError('تغییر ورود کامل نشد و بازگردانی همهٔ سرویس‌ها تأیید نشد؛ فایل خصوصی credential-recovery.json و وضعیت واقعی سرویس‌ها باید در میزبان بررسی شود. ' + detail) from None
                raise CredentialsError('تغییر اطلاعات ورود انجام نشد؛ وضعیت قبلی حفظ یا بازگردانی شد. ' + detail) from None
        return {'completed': True, 'target': target, 'username': username,
                'changed': [name for name, active in (('web', web), ('grafana', grafana)) if active],
                'relogin_required': bool(web), 'redis_changed': False}
