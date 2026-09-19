"""Regression checks for the public gateway's routing/security contract.

The end-to-end Nginx/WebSocket test still belongs on the Ubuntu Docker host;
these checks validate the actual shipped route expression against the allocator.
"""
from pathlib import Path
import re
import unittest

from generate_farm import generate_one
from ops.device_ids import ADB_PORT_BASE, DEVICE_LIMIT, device_id


ROOT = Path(__file__).resolve().parents[1]
CONFIG = (ROOT / 'images/gateway/nginx.conf').read_text(encoding='utf-8')
COMPOSE = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')


class GatewayTests(unittest.TestCase):
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

    def test_public_routes_share_auth_and_do_not_forward_credentials(self):
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


if __name__ == '__main__':
    unittest.main()
