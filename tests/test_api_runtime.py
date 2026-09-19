"""Opt-in Linux integration for the real private Gunicorn API.

Run as root after installing Gunicorn and apache2-utils:
    FARM_API_RUNTIME_TEST=1 python -m unittest discover -s tests -p test_api_runtime.py -v

State lives inside a temporary /run directory. The optional console test uses
an isolated Docker container; no device mutation, system service change, or
production configuration is used.
"""
import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import yaml

from ops.secureio import atomic_json
from services.api.jobs import JobQueue


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_AVAILABLE = (
    os.environ.get('FARM_API_RUNTIME_TEST') == '1'
    and sys.platform == 'linux'
    and os.geteuid() == 0
    and importlib.util.find_spec('gunicorn') is not None
    and shutil.which('htpasswd') is not None
)
SKIP_REASON = 'requires opt-in FARM_API_RUNTIME_TEST=1, Linux root, Gunicorn and htpasswd'


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('farm.example.test', timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


@unittest.skipUnless(RUNTIME_AVAILABLE, SKIP_REASON)
class GunicornRuntimeTests(unittest.TestCase):
    def setUp(self):
        # /tmp has a writable ancestor, which the production socket guard
        # correctly rejects. /run provides a protected root-owned parent.
        self.temporary = tempfile.TemporaryDirectory(prefix='farm-api-test-', dir='/run')
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.socket_directory = self.root / 'socket'
        self.socket_directory.mkdir(mode=0o755)
        self.socket = self.socket_directory / 'control.sock'
        self.origin = 'https://farm.example.test'
        self.password = 'isolated-runtime-test-password'
        auth_file = self.root / 'users.htpasswd'
        result = subprocess.run(['htpasswd', '-nBi', 'operator'], input=self.password + '\n',
                                text=True, capture_output=True, check=True, timeout=10)
        auth_file.write_text(result.stdout, encoding='utf-8')
        auth_file.chmod(0o600)
        self.header = 'Basic ' + base64.b64encode(f'operator:{self.password}'.encode()).decode()
        config = self.root / 'api.json'
        atomic_json(config, {'schema_version': 1, 'provisioner_config': str(self.root / 'missing-provisioner.json'),
                             'auth_file': str(auth_file), 'allowed_origins': [self.origin],
                             'socket_path': str(self.socket), 'state_dir': str(self.root / 'jobs')})
        self.process = subprocess.Popen([sys.executable, '-m', 'services.api.server', '--config', str(config)],
                                        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        start_new_session=True)
        self.addCleanup(self.stop_server)
        self.wait_ready()

    def stop_server(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.process.stderr.close()

    def request(self, path='/api/v1/health', method='GET', body=None, headers=None):
        connection = UnixConnection(self.socket)
        values = {'Authorization': self.header}
        if headers:
            values.update(headers)
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode()
            values.setdefault('Content-Type', 'application/json')
        try:
            connection.request(method, path, body=encoded, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def wait_ready(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail('isolated Gunicorn process exited before becoming ready: ' +
                          self.process.stderr.read(4096).decode(errors='replace'))
            try:
                if self.request()[0] == 200:
                    return
            except (OSError, http.client.HTTPException, ValueError):
                pass
            time.sleep(.1)
        self.fail('isolated Gunicorn API did not become ready within 15 seconds')

    def test_real_socket_auth_snapshot_and_csrf_without_host_operations(self):
        info = self.socket.lstat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 101, 0o660))
        code, headers, _ = self.request(headers={'Authorization': ''})
        self.assertEqual(code, 401)
        self.assertTrue(headers['WWW-Authenticate'].startswith('Basic '))
        bad = 'Basic ' + base64.b64encode(b'operator:wrong-password').decode()
        self.assertEqual(self.request(headers={'Authorization': bad})[0], 401)
        self.assertEqual(self.request()[2]['status'], 'ok')
        code, headers, snapshot = self.request('/api/v1/snapshot')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(snapshot['devices'], [])
        self.assertIsNone(snapshot['resources'])
        self.assertEqual(snapshot['errors'][0]['component'], 'configuration')
        csrf = snapshot['csrf_token']
        post_headers = {'Origin': self.origin, 'X-Farm-CSRF': csrf, 'Idempotency-Key': str(uuid.uuid4())}
        # Unsupported action is rejected before configuration loading, so this
        # integration can never invoke Docker or a real provisioner operation.
        body = {'action': 'not-a-real-action'}
        self.assertEqual(self.request('/api/v1/jobs', 'POST', body,
                                      dict(post_headers, Origin='https://other.example.test'))[0], 403)
        self.assertEqual(self.request('/api/v1/jobs', 'POST', body,
                                      dict(post_headers, **{'X-Farm-CSRF': 'wrong'}))[0], 403)
        self.assertEqual(self.request('/api/v1/jobs', 'POST', body, post_headers)[0], 400)
        self.assertEqual(self.request('/api/v1/snapshot')[2]['jobs'], [])

    def test_real_worker_crash_restarts_with_fresh_csrf_and_private_socket(self):
        before = self.request('/api/v1/snapshot')[2]['csrf_token']
        child_file = Path(f'/proc/{self.process.pid}/task/{self.process.pid}/children')
        workers = child_file.read_text().split()
        self.assertEqual(len(workers), 1)
        os.kill(int(workers[0]), signal.SIGKILL)
        deadline = time.monotonic() + 15
        after = None
        while time.monotonic() < deadline:
            try:
                status, _, body = self.request('/api/v1/snapshot')
                if status == 200 and body['csrf_token'] != before:
                    after = body['csrf_token']
                    break
            except (OSError, http.client.HTTPException, ValueError):
                pass
            time.sleep(.1)
        self.assertIsNotNone(after, 'Gunicorn did not replace the killed worker')
        self.assertEqual(stat.S_IMODE(self.socket.stat().st_mode), 0o660)
        self.assertEqual(self.request()[0], 200)

    @unittest.skipUnless(os.environ.get('FARM_CONSOLE_TEST_IMAGE') and shutil.which('docker'),
                         'requires FARM_CONSOLE_TEST_IMAGE and Docker')
    def test_readonly_console_proxies_authenticated_api_over_unix_socket(self):
        name = 'farm-api-console-test-' + uuid.uuid4().hex
        image = os.environ['FARM_CONSOLE_TEST_IMAGE']
        # Exercise the actual deployment tmpfs list. An independent list missed
        # a /var/run -> /run alias that hid the socket bind on Alpine images.
        core = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        temporary_mounts = [value for mount in core['services']['console']['tmpfs']
                            for value in ('--tmpfs', mount)]
        result = subprocess.run(
            ['docker', 'run', '--detach', '--rm', '--name', name, '--network', 'none',
             '--read-only', '--user', '101:101', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
             *temporary_mounts,
             '--mount', f'type=bind,source={self.socket_directory},target=/run/farm-api,readonly', image],
            capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, 'isolated console container could not start')
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                health = subprocess.run(['docker', 'exec', name, 'wget', '-q', '-O', '-',
                                         'http://127.0.0.1:8080/healthz'],
                                        capture_output=True, text=True, timeout=5)
                if health.returncode == 0 and health.stdout.strip() == 'ok':
                    break
                time.sleep(.1)
            else:
                self.fail('read-only console Nginx did not become healthy')
            denied = subprocess.run(['docker', 'exec', name, 'wget', '-S', '-O', '-',
                                     'http://127.0.0.1:8080/api/v1/health'],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn('401 Unauthorized', denied.stderr)
            # The fixture credential is supplied over stdin, never command-line
            # arguments. The fixed shell program contains no interpolated input.
            script = 'IFS= read -r auth; exec wget -q -O - --header "$auth" http://127.0.0.1:8080/api/v1/health'
            allowed = subprocess.run(['docker', 'exec', '-i', name, 'sh', '-c', script],
                                     input=f'Authorization: {self.header}\n', capture_output=True,
                                     text=True, timeout=10)
            self.assertEqual(allowed.returncode, 0, 'console could not authenticate through the Unix socket')
            self.assertEqual(json.loads(allowed.stdout)['status'], 'ok')
        finally:
            subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=15)


@unittest.skipUnless(RUNTIME_AVAILABLE, SKIP_REASON)
class QueueSingletonRuntimeTests(unittest.TestCase):
    def test_second_process_cannot_recover_or_execute_an_active_owners_job(self):
        with tempfile.TemporaryDirectory(prefix='farm-queue-test-', dir='/run') as folder:
            directory = Path(folder) / 'jobs'
            entered, finish = threading.Event(), threading.Event()

            def execute(_payload):
                entered.set()
                if not finish.wait(timeout=15):
                    raise RuntimeError('test worker was not released')
                return {'completed': True}

            queue = JobQueue(directory, execute)
            try:
                queue.submit(str(uuid.uuid4()), {'action': 'check', 'device': 'num01'}, 'operator')
                self.assertTrue(entered.wait(timeout=5))
                script = (
                    'from pathlib import Path\n'
                    'import sys\n'
                    'from services.api.jobs import JobQueue\n'
                    'try:\n'
                    '    q=JobQueue(Path(sys.argv[1]),lambda job:{"unexpected":True})\n'
                    'except RuntimeError:\n'
                    '    print("ownership-blocked")\n'
                    '    sys.exit(0)\n'
                    'q.close()\n'
                    'sys.exit(3)\n'
                )
                result = subprocess.run([sys.executable, '-c', script, str(directory)], cwd=ROOT,
                                        capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0)
                self.assertIn('ownership-blocked', result.stdout)
                # The rejected owner must not run startup recovery, which would
                # otherwise mark another process's running job interrupted.
                self.assertEqual(queue.list()[0]['state'], 'running')
            finally:
                finish.set()
                queue.close()
            replacement = JobQueue(directory, lambda _job: {'unexpected': True})
            try:
                self.assertTrue(replacement.healthy())
                self.assertEqual(replacement.list()[0]['state'], 'succeeded')
            finally:
                replacement.close()


if __name__ == '__main__':
    unittest.main()
