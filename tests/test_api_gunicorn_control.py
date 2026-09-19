"""Linux regression for Gunicorn's auxiliary socket on a protected home.

Run with FARM_API_RUNTIME_TEST=1 as root, using the pinned API requirements.
This launches the real API with an unwritable HOME and no XDG runtime override;
it must still serve its private authenticated HTTP socket without a gunicornc
control server. No production service or device is changed.
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from ops.secureio import atomic_json


ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = (
    os.environ.get('FARM_API_RUNTIME_TEST') == '1'
    and sys.platform == 'linux'
    and os.geteuid() == 0
    and importlib.util.find_spec('gunicorn') is not None
)


@unittest.skipUnless(AVAILABLE, 'requires opt-in FARM_API_RUNTIME_TEST=1, Linux root and Gunicorn')
class GunicornProtectedHomeTests(unittest.TestCase):
    def test_private_http_listener_works_without_writing_a_home_control_socket(self):
        with tempfile.TemporaryDirectory(prefix='farm-gunicorn-home-', dir='/run') as folder:
            root = Path(folder)
            listener = root / 'control.sock'
            config = root / 'api.json'
            atomic_json(config, {
                'schema_version': 1,
                'provisioner_config': str(root / 'unused-provisioner.json'),
                'auth_file': str(root / 'unused-users.htpasswd'),
                'allowed_origins': ['https://farm.example.test'],
                'socket_path': str(listener),
                'state_dir': str(root / 'jobs'),
            })
            environment = dict(os.environ, HOME='/proc/self', PYTHONDONTWRITEBYTECODE='1')
            environment.pop('XDG_RUNTIME_DIR', None)
            with (root / 'stderr.log').open('w+b') as errors:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'services.api.server', '--config', str(config)],
                    cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=errors,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 15
                    result = None
                    while time.monotonic() < deadline and process.poll() is None:
                        connection = http.client.HTTPConnection('farm.example.test', timeout=2)
                        try:
                            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            connection.sock.settimeout(2)
                            connection.sock.connect(str(listener))
                            connection.request('GET', '/api/v1/health')
                            response = connection.getresponse()
                            result = (response.status, dict(response.getheaders()), json.loads(response.read()))
                            break
                        except (OSError, ValueError, http.client.HTTPException):
                            time.sleep(.1)
                        finally:
                            connection.close()
                    self.assertIsNotNone(result, 'managed API listener never became available')
                    self.assertEqual(result[0], 401)
                    self.assertIn('WWW-Authenticate', result[1])
                    info = listener.stat()
                    self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 101, 0o660))
                    # The optional control thread is started just after worker
                    # creation. Give it time to report a failure if re-enabled.
                    time.sleep(1)
                    self.assertIsNone(process.poll())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
                errors.seek(0)
                output = errors.read().decode(errors='replace')
                self.assertNotIn('Control server error', output)
                self.assertNotIn('Control socket listening', output)
                self.assertNotIn('/proc/self/.gunicorn', output)


if __name__ == '__main__':
    unittest.main()
