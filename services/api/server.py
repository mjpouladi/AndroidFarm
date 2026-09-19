"""Authenticated WSGI control API, served by Gunicorn on a private Unix socket.

No TCP listener, shell endpoint, arbitrary command, Docker API forwarding or
credential retrieval is exposed. The browser uses the farm's existing Basic
Auth; the API verifies it independently, including for direct internal calls.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

from ops.secureio import read_private_json
from .jobs import JobQueue, QueueConflict
from .authstate import auth_revision

MAX_BODY = 32768
# Raw APK uploads; the console and gateway Nginx enforce the same ceiling.
MAX_UPLOAD = 256 * 1024 ** 2
UPLOAD_ROUTE = '/api/v1/artifacts/upload'
UPLOAD_TYPES = frozenset({'application/vnd.android.package-archive', 'application/octet-stream'})
USER_RE = re.compile(r'[A-Za-z0-9._][A-Za-z0-9._-]{0,63}\Z')
JOB_ROUTE = re.compile(r'/api/v1/jobs/([0-9a-f-]{36})/cancel\Z')


def _header_text(value, maximum):
    """Decode a percent-encoded ASCII header into bounded text, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = unquote(value, errors='strict')
    except (UnicodeDecodeError, ValueError):
        return None
    if not decoded.strip() or len(decoded) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return None
    return decoded.strip()


class BasicAuth:
    def __init__(self, path: Path, *, runner=subprocess.run):
        self.path = path
        self.runner = runner
        self.lock = threading.Lock()
        self.cache = {}
        self.salt = secrets.token_bytes(32)
        self.verifiers = threading.BoundedSemaphore(4)

    def revision(self):
        return auth_revision(self.path)

    def verify(self, header):
        if not isinstance(header, str) or len(header) > 8192 or not header.startswith('Basic '):
            return None
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode('utf-8')
            user, password = decoded.split(':', 1)
            if not USER_RE.fullmatch(user) or not password or any(c in password for c in '\r\n\x00'):
                return None
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or self.path.is_symlink():
                return None
            if os.name == 'posix' and (info.st_uid not in {0, 9999} or info.st_mode & 0o077):
                return None
            for parent in self.path.parents:
                if parent.is_symlink():
                    return None
            version = (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            digest = hmac.new(self.salt, header.encode(), hashlib.sha256).digest()
            key = (version, digest)
            with self.lock:
                cached = self.cache.get(key)
                if cached and cached[0] > time.monotonic():
                    return cached[1]
            if not self.verifiers.acquire(blocking=False):
                return None
            try:
                result = self.runner(['htpasswd', '-vi', str(self.path), user],
                                     input=password + '\n', text=True, capture_output=True, timeout=5)
            finally:
                self.verifiers.release()
            valid = user if result.returncode == 0 else None
            with self.lock:
                if len(self.cache) >= 128:
                    self.cache.clear()
                self.cache[key] = (time.monotonic() + (20 if valid else 2), valid)
            return valid
        except (OSError, ValueError, UnicodeError, binascii.Error, subprocess.SubprocessError):
            return None


class Application:
    def __init__(self, operations, jobs, auth, allowed_origins):
        self.operations = operations
        self.jobs = jobs
        self.auth = auth
        self.allowed_origins = frozenset(allowed_origins)
        self.csrf = secrets.token_urlsafe(32)
        self.snapshot_lock = threading.Lock()
        self.cached_snapshot = None
        self.snapshot_time = 0.0

    @staticmethod
    def response(start_response, status, value, headers=()):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        names = {200: 'OK', 202: 'Accepted', 400: 'Bad Request', 401: 'Unauthorized',
                 403: 'Forbidden', 404: 'Not Found', 409: 'Conflict', 413: 'Payload Too Large',
                 415: 'Unsupported Media Type', 500: 'Internal Server Error', 503: 'Service Unavailable'}
        start_response(f'{status} {names[status]}', [
            ('Content-Type', 'application/json; charset=utf-8'), ('Content-Length', str(len(body))),
            ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'), *headers])
        return [body]

    def upload(self, env, user, content_type, send, error):
        """Stage a raw APK body privately and queue its verified import.

        The body is streamed to a root-private staging file; ``aapt`` and
        ``apksigner`` run later inside the durable queue, never on the request
        thread, and the browser only ever references its own staging token.
        """
        if content_type not in UPLOAD_TYPES:
            return error(415, 'apk_required', 'فایل باید یک بستهٔ APK باشد.')
        length = env.get('CONTENT_LENGTH', '')
        if not re.fullmatch(r'[0-9]{1,10}', length):
            return error(400, 'invalid_length', 'اندازهٔ فایل معتبر نیست.')
        if int(length) > MAX_UPLOAD:
            return error(413, 'body_too_large', 'حجم APK بیش از سقف مجاز ۲۵۶ مگابایت است.')
        params = {}
        label = _header_text(env.get('HTTP_X_FARM_ARTIFACT_LABEL'), 80)
        filename = _header_text(env.get('HTTP_X_FARM_ARTIFACT_FILENAME'), 160)
        identifier = env.get('HTTP_X_FARM_ARTIFACT_ID', '')
        permissions = [item for item in env.get('HTTP_X_FARM_ARTIFACT_PERMISSIONS', '').split(',') if item]
        if label:
            params['label'] = label
        if filename:
            params['filename'] = filename
        if identifier:
            params['id'] = identifier
        if permissions:
            params['permissions'] = permissions
        if env.get('HTTP_X_FARM_ARTIFACT_ALLOW_SIGNER_CHANGE') == 'true':
            params['allow_signer_change'] = True
        staged = self.operations.stage_upload(env['wsgi.input'], int(length))
        try:
            normalized = self.operations.validate_job({'action': 'artifact-import',
                                                       'params': dict(params, upload=staged['upload'])})
            job, created = self.jobs.submit(env.get('HTTP_IDEMPOTENCY_KEY'), normalized, user)
        except BaseException:
            self.operations.discard_upload(staged['upload'])
            raise
        return send(202 if created else 200, {'job': job})

    def __call__(self, env, start_response):
        def send(status, value, headers=()):
            return self.response(start_response, status, value, headers)

        def error(status, code, message):
            return send(status, {'error': {'code': code, 'message': message}})

        try:
            user = self.auth.verify(env.get('HTTP_AUTHORIZATION', ''))
            if not user:
                return send(401, {'error': {'code': 'authentication_required',
                                          'message': 'ورود با حساب فارم لازم است.'}},
                            [('WWW-Authenticate', 'Basic realm="traefik", charset="UTF-8"')])
            method, path = env.get('REQUEST_METHOD'), env.get('PATH_INFO', '')
            if env.get('QUERY_STRING'):
                return error(400, 'invalid_query', 'این مسیر پارامتر query نمی‌پذیرد.')
            if method == 'GET' and path == '/api/v1/health':
                return send(200 if self.jobs.healthy() else 503,
                            {'status': 'ok' if self.jobs.healthy() else 'degraded'})
            if method == 'GET' and path == '/api/v1/snapshot':
                # Coalesce concurrent browser polls; no detached refresh can
                # overwrite a newer response with older state.
                with self.snapshot_lock:
                    if self.cached_snapshot is None or time.monotonic() - self.snapshot_time >= 3:
                        self.cached_snapshot = self.operations.snapshot()
                        self.cached_snapshot['sampling_started_at'] = self.cached_snapshot.get('collected_at')
                        self.cached_snapshot['collected_at'] = int(time.time())
                        self.snapshot_time = time.monotonic()
                    snapshot = dict(self.cached_snapshot)
                snapshot.update(schema_version=1, csrf_token=self.csrf, jobs=self.jobs.list())
                if not self.jobs.healthy():
                    snapshot['errors'] = [*snapshot.get('errors', []),
                                          {'component': 'queue', 'message': 'صف عملیات موقتاً در دسترس نیست.'}]
                return send(200, snapshot)
            if method != 'POST' or (path != '/api/v1/jobs' and path != UPLOAD_ROUTE and not JOB_ROUTE.fullmatch(path)):
                return error(404, 'not_found', 'مسیر API پیدا نشد.')
            if not self.jobs.healthy():
                return error(503, 'queue_unavailable', 'صف عملیات موقتاً در دسترس نیست؛ درخواست جدید ثبت نشد.')
            if (env.get('HTTP_ORIGIN') not in self.allowed_origins or
                    env.get('HTTP_SEC_FETCH_SITE') == 'cross-site' or
                    not hmac.compare_digest(env.get('HTTP_X_FARM_CSRF', '').encode('utf-8'), self.csrf.encode('ascii'))):
                return error(403, 'csrf_rejected', 'مبدأ یا توکن درخواست معتبر نیست؛ صفحه را تازه‌سازی کنید.')
            content_type = env.get('CONTENT_TYPE', '').split(';')[0].strip().lower()
            if path == UPLOAD_ROUTE:
                return self.upload(env, user, content_type, send, error)
            if content_type != 'application/json':
                return error(415, 'json_required', 'درخواست باید JSON باشد.')
            length = env.get('CONTENT_LENGTH', '')
            if not re.fullmatch(r'[0-9]{1,8}', length):
                return error(400, 'invalid_length', 'اندازهٔ درخواست معتبر نیست.')
            if int(length) > MAX_BODY:
                return error(413, 'body_too_large', 'درخواست بیش از حد بزرگ است.')
            raw = env['wsgi.input'].read(int(length))
            if len(raw) != int(length):
                return error(400, 'incomplete_body', 'درخواست کامل دریافت نشد.')
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                return error(400, 'invalid_json', 'ساختار JSON معتبر نیست.')
            if not isinstance(value, dict):
                return error(400, 'invalid_request', 'درخواست باید یک شیء JSON باشد.')
            route = JOB_ROUTE.fullmatch(path)
            if route:
                if value:
                    return error(400, 'invalid_request', 'لغو درخواست دادهٔ اضافه نمی‌پذیرد.')
                return send(200, {'job': self.jobs.cancel(route.group(1))})
            if value.get('action') in ('credential-rotate', 'proxy-credentials'):
                params = value.get('params')
                current = params.get('current_password') if isinstance(params, dict) else None
                if not isinstance(current, str) or not current or len(current) > 4096:
                    return error(403, 'reauthentication_required', 'برای تغییر رمز، رمز فعلی ورود به فارم را وارد کنید.')
                supplied = 'Basic ' + base64.b64encode(f'{user}:{current}'.encode('utf-8')).decode('ascii')
                revision = self.auth.revision()
                if self.auth.verify(supplied) != user or revision != self.auth.revision():
                    return error(403, 'reauthentication_failed', 'رمز فعلی ورود به فارم صحیح نیست.')
            normalized = self.operations.validate_job(value)
            if value.get('action') in ('credential-rotate', 'proxy-credentials'):
                normalized = dict(normalized, params={k: v for k, v in normalized['params'].items()
                                                      if k != 'current_password'}, auth_revision=revision)
            job, created = self.jobs.submit(env.get('HTTP_IDEMPOTENCY_KEY'), normalized, user)
            return send(202 if created else 200, {'job': job})
        except QueueConflict as exc:
            return error(409, 'conflict', str(exc))
        except KeyError:
            return error(404, 'not_found', 'دستگاه یا درخواست پیدا نشد.')
        except ValueError as exc:
            # validate_job uses controlled field descriptions, never payloads.
            return error(400, 'invalid_request', str(exc)[:400])
        except Exception:
            return error(503, 'host_unavailable', 'کنترل‌پلین پاسخ نداد؛ سرویس android-farm-api را در میزبان بررسی کنید.')


def load_settings(path):
    settings = read_private_json(path, 'API configuration')
    expected = {'schema_version', 'provisioner_config', 'auth_file', 'allowed_origins', 'socket_path', 'state_dir'}
    if not isinstance(settings, dict) or set(settings) != expected or settings['schema_version'] != 1:
        raise ValueError('invalid API configuration schema')
    if not isinstance(settings['allowed_origins'], list) or not settings['allowed_origins']:
        raise ValueError('API requires explicit allowed origins')
    for origin in settings['allowed_origins']:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or
                parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('invalid API origin')
    for key in ('provisioner_config', 'auth_file', 'socket_path', 'state_dir'):
        if not isinstance(settings[key], str) or not Path(settings[key]).is_absolute():
            raise ValueError('API configuration paths must be absolute')
    return settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/android-farm/api.json'))
    settings = load_settings(parser.parse_args(argv).config)
    if os.name != 'posix' or os.geteuid() != 0:
        raise RuntimeError('API must run as the managed root service on the Ubuntu host')
    socket_path = Path(settings['socket_path'])
    parent = socket_path.parent
    for directory in (parent, *parent.parents):
        info = directory.lstat()
        if directory.is_symlink() or not directory.is_dir() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('API socket parent must be a protected root-owned directory')
    if socket_path.exists() or socket_path.is_symlink():
        if socket_path.is_symlink() or not stat.S_ISSOCK(socket_path.lstat().st_mode):
            raise RuntimeError('refusing to replace non-socket API path')
    from gunicorn.app.base import BaseApplication

    class Server(BaseApplication):
        def load_config(self):
            options = {'bind': f'unix:{socket_path}', 'workers': 1, 'worker_class': 'gthread',
                       'threads': 8, 'timeout': 120, 'graceful_timeout': 30,
                       'umask': 0o077, 'accesslog': None, 'errorlog': '-',
                       # Gunicorn 25.1+ otherwise creates a separate gunicornc
                       # management socket under $HOME/.gunicorn. Lifecycle is
                       # managed by systemd, and ProtectHome stays enabled.
                       # This does not disable the authenticated API listener.
                       'control_socket_disable': True,
                       'limit_request_line': 4094, 'limit_request_fields': 40,
                       'when_ready': self.socket_ready}
            for key, value in options.items():
                self.cfg.set(key, value)

        @staticmethod
        def socket_ready(_server):
            os.chown(socket_path, 0, 101)
            os.chmod(socket_path, 0o660)

        def load(self):
            # Loaded after fork. Exactly one worker owns the durable job queue.
            from .operations import Operations
            operations = Operations(config_path=Path(settings['provisioner_config']),
                                    auth_file=Path(settings['auth_file']))
            jobs = JobQueue(Path(settings['state_dir']), operations.execute)
            return Application(operations, jobs, BasicAuth(Path(settings['auth_file'])), settings['allowed_origins'])

    Server().run()


if __name__ == '__main__':
    main()
