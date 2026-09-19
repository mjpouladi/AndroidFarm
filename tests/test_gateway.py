"""Regression checks for the public gateway's routing/security contract.

Set FARM_GATEWAY_TEST_IMAGE to a locally built gateway image to also run the
read-only startup/health smoke test on a Linux Docker host. WebSocket routing
still requires a running device; route expressions are tested offline here.
"""
import os
from pathlib import Path
import re
import subprocess
import unittest

import yaml

from generate_farm import generate_one
from ops.device_ids import ADB_PORT_BASE, DEVICE_LIMIT, device_id


ROOT = Path(__file__).resolve().parents[1]
CONFIG = (ROOT / 'images/gateway/nginx.conf').read_text(encoding='utf-8')
COMPOSE = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')


class GatewayTests(unittest.TestCase):
    def test_console_tmpfs_cannot_hide_the_authenticated_socket_bind(self):
        console = yaml.safe_load(COMPOSE)['services']['console']
        socket_directory = '/run/farm-api'
        self.assertIn('/run/android-farm-api:' + socket_directory + ':ro', console['volumes'])
        # Alpine's /var/run symlink means distinct strings may mount the same
        # parent. This reproduces the production ENOENT configuration failure.
        for mount in console.get('tmpfs', []):
            destination = mount.split(':', 1)[0].rstrip('/') or '/'
            if destination == '/var/run' or destination.startswith('/var/run/'):
                destination = '/run' + destination[len('/var/run'):]
            self.assertFalse(destination == '/' or destination == socket_directory or
                             socket_directory.startswith(destination + '/') or
                             destination.startswith(socket_directory + '/'), mount)

    def test_all_nginx_runtime_paths_use_writable_tmpfs(self):
        gateway = yaml.safe_load(COMPOSE)['services']['gateway']
        self.assertTrue(gateway['read_only'])
        self.assertEqual(gateway['user'], '101:101')
        self.assertEqual(gateway['tmpfs'], ['/tmp'])
        # Nginx initializes even unused upstream module paths at startup.
        # Omitting any directive silently restores an unwritable image path.
        for directive in ('pid', 'client_body_temp_path', 'proxy_temp_path',
                          'fastcgi_temp_path', 'uwsgi_temp_path', 'scgi_temp_path'):
            with self.subTest(directive=directive):
                values = re.findall(r'^\s*' + directive + r'\s+([^;]+);', CONFIG, re.MULTILINE)
                self.assertEqual(len(values), 1)
                path = values[0].split()[0]
                self.assertTrue(path.startswith('/tmp/'), path)
                self.assertNotIn('..', path.split('/'))

    def screen_route(self):
        expression = re.search(r'location ~ "([^"]+)"', CONFIG).group(1)
        return re.compile(re.sub(r'\(\?<([a-z_]+)>', r'(?P<\1>', expression))

    def test_route_accepts_every_allocatable_device(self):
        route = self.screen_route()
        for index in range(1, DEVICE_LIMIT + 1):
            name = device_id(index)
            with self.subTest(device=name):
                match = route.fullmatch(f'/d/{name}/websockify')
                self.assertIsNotNone(match)
                self.assertEqual(match['device'], name)
                self.assertEqual(match['screen_path'], '/websockify')

    def test_route_rejects_noncanonical_ids_and_host_injection(self):
        route = self.screen_route()
        invalid = (
            'num00', 'num1', 'num001', 'num010', 'num8193', 'num9999',
            'dev01', 'NUM01', 'localhost', '127.0.0.1', 'num01:2375',
            'num01@169.254.169.254', 'num01.example.com', '../num01',
            'num01%2f..', 'num01?url=http://localhost',
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(route.fullmatch(f'/d/{value}/websockify'))
        self.assertIn('location = /d { return 404; }', CONFIG)
        self.assertIn('location /d/ { return 404; }', CONFIG)
        self.assertIn('set $screen_upstream http://screen-$device:6080;', CONFIG)

    def test_ambiguous_paths_are_rejected_without_blocking_query_values(self):
        expressions = re.findall(r'^\s+~(\*?)([^\s]+) 1;', CONFIG, re.MULTILINE)
        patterns = [re.compile(value, re.IGNORECASE if flag else 0)
                    for flag, value in expressions]
        self.assertEqual(len(patterns), 2)
        for value in ('/d/num01/../metrics/', '/d/num01/./vnc.html',
                      '/d/num01/%2e%2e/metrics/', '/d/num01%2F/anything',
                      '/d/num01/%5canything', '/d/num01/x%3Fy',
                      '/d/num01/%252e%252e/private', '/d/num01/%00x'):
            with self.subTest(value=value):
                self.assertTrue(any(pattern.search(value) for pattern in patterns))
        self.assertFalse(any(pattern.search('/d/num01/vnc.html?path=d%2Fnum01%2Fwebsockify')
                             for pattern in patterns))
        self.assertIn('if ($unsafe_path) { return 400; }', CONFIG)

    def test_public_routes_share_auth_and_only_api_forwards_credentials(self):
        public = CONFIG.split('listen 8080 default_server;', 1)[1].split(
            '# Container-only liveness listener', 1)[0]
        self.assertIn('auth_basic "Android Farm";', public)
        self.assertIn('auth_basic_user_file /run/gateway-private/farm-users.htpasswd;', public)
        self.assertNotIn('auth_basic off', public)
        self.assertNotIn('/healthz', public)
        self.assertIn('proxy_set_header Authorization "";', public)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', public)
        self.assertIn('proxy_set_header Connection $connection_upgrade;', public)
        self.assertIn('resolver 127.0.0.11 ipv6=off valid=10s;', CONFIG)
        self.assertIn('listen 127.0.0.1:8081;', CONFIG)
        api = public.split('location ^~ /api/ {', 1)[1].split('}', 1)[0]
        self.assertIn('proxy_set_header Authorization $http_authorization;', api)
        self.assertIn('proxy_set_header Host $http_host;', api)
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme;', api)
        self.assertIn('proxy_pass $console_api_upstream$request_uri;', api)
        self.assertEqual(public.count('proxy_set_header Authorization $http_authorization;'), 1)

    def test_console_api_is_unix_only_and_reauthenticates_preserved_credentials(self):
        console = yaml.safe_load(COMPOSE)['services']['console']
        self.assertIn('/run/android-farm-api:/run/farm-api:ro', console['volumes'])
        self.assertNotIn('ports', console)
        self.assertNotIn('docker.sock', repr(console))
        self.assertIn('traefik.http.routers.farm-console.middlewares=farm-console-auth@file',
                      console['labels'])
        middleware = yaml.safe_load((ROOT / 'traefik/farm-auth.yml').read_text())['http']['middlewares']
        self.assertFalse(middleware['farm-console-auth']['basicAuth']['removeHeader'])
        self.assertTrue(middleware['farm-auth']['basicAuth']['removeHeader'])
        nginx = (ROOT / 'web/nginx.conf').read_text()
        self.assertIn('proxy_pass http://unix:/run/farm-api/control.sock;', nginx)
        self.assertIn('proxy_set_header Authorization $http_authorization;', nginx)
        self.assertIn('proxy_next_upstream off;', nginx)
        self.assertIn('resolver 127.0.0.11 ipv6=off valid=10s;', nginx)
        self.assertIn('proxy_pass $screen_gateway$request_uri;', nginx)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', nginx)
        self.assertIn('proxy_set_header Connection $console_connection_upgrade;', nginx)
        screen = nginx.split('location ^~ /d/ {', 1)[1].split('}', 1)[0]
        self.assertIn('add_header X-Content-Type-Options nosniff always;', screen)
        self.assertIn('add_header Referrer-Policy same-origin always;', screen)
        self.assertIn('add_header X-Frame-Options SAMEORIGIN always;', screen)
        self.assertNotIn('add_header Content-Security-Policy', screen)
        self.assertIn("script-src 'self'", nginx.split('location /api/', 1)[0])

    def test_deployment_defaults_do_not_publish_public_http_or_adb(self):
        self.assertIn('${FARM_HTTP_BIND:-127.0.0.1}:${FARM_HTTP_PORT:-18080}:8080', COMPOSE)
        self.assertGreater(18080, ADB_PORT_BASE + DEVICE_LIMIT)
        gateway = COMPOSE.split('  gateway:\n', 1)[1].split('\n  prometheus:', 1)[0]
        self.assertIn('user: "101:101"', gateway)
        self.assertIn('cap_drop: [ALL]', gateway)
        self.assertIn('gateway-secrets:/run/gateway-private:ro', gateway)
        self.assertNotIn('docker.sock', gateway)
        self.assertNotIn(':8081"', gateway)
        self.assertIn('chown 101:101 /private/farm-users.htpasswd.next', COMPOSE)
        self.assertIn('chmod 0400 /private/farm-users.htpasswd.next', COMPOSE)
        self.assertIn('GF_SERVER_SERVE_FROM_SUB_PATH: ${GRAFANA_SERVE_FROM_SUB_PATH:-false}', COMPOSE)
        device = generate_one(1)
        self.assertEqual(device['services']['screen-num01']['labels']['traefik.enable'],
                         '${FARM_TRAEFIK_ENABLED:-true}')
        self.assertEqual(device['services']['proxy-num01']['ports'], ['127.0.0.1:5551:5555'])

    def test_auth_rotation_recreates_secret_copy_and_gateway(self):
        secret_init = COMPOSE.split('  gateway-secret-init:\n', 1)[1].split('\n  gateway:', 1)[0]
        gateway = COMPOSE.split('  gateway:\n', 1)[1].split('\n  prometheus:', 1)[0]
        for service in (secret_init, gateway):
            self.assertIn('farm.auth.revision=${FARM_HTTP_AUTH_REVISION:-manual}', service)
        self.assertIn('gateway-secret-init:\n        condition: service_completed_successfully', gateway)

    @unittest.skipUnless(os.environ.get('FARM_GATEWAY_TEST_IMAGE'),
                         'requires a locally built image and Linux Docker engine')
    def test_read_only_container_starts_and_serves_health(self):
        # No published ports, host mounts, credentials or external networking.
        # Exercise actual startup: nginx -t alone does not exercise every mkdir.
        result = subprocess.run([
            'docker', 'run', '--rm', '--pull=never', '--network=none',
            '--read-only', '--tmpfs', '/tmp', '--user', '101:101',
            '--cap-drop=ALL', '--security-opt', 'no-new-privileges:true',
            '--entrypoint', 'sh', os.environ['FARM_GATEWAY_TEST_IMAGE'], '-ec',
            'nginx; trap "nginx -s quit" EXIT; '
            'wget -q -O- http://127.0.0.1:8081/healthz',
        ], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ok')


if __name__ == '__main__':
    unittest.main()
