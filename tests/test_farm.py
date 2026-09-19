import importlib.util
import json
import unittest
from pathlib import Path
from generate_farm import generate, generate_one
from ops.farmctl import assert_capacity
from ops.device_ids import DEVICE_LIMIT, network_plan

spec = importlib.util.spec_from_file_location('proxy_config', Path('images/proxy/configure.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FarmTests(unittest.TestCase):
    def test_delivered_files_match_generator(self):
        self.assertEqual(json.loads(Path('docker-compose.farm.yml').read_text()), generate(1))
        core = Path('docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('farm-anchor:', core)
        self.assertIn('console:', core)
        self.assertIn('grafana-secret-init:', core)
        self.assertIn('user: "472:0"', core)
        self.assertIn('condition: service_completed_successfully', core)
        self.assertIn('GF_SECURITY_ADMIN_PASSWORD__FILE: /run/grafana-private/admin-password', core)
        self.assertIn('chown 472:0 /private/admin-password.next', core)
        self.assertIn('chmod 0400 /private/admin-password.next', core)
        self.assertIn('chown -R 472:0 /grafana-data', core)
        self.assertIn('- grafana-data:/grafana-data', core)

    def test_scale_and_network_isolation(self):
        doc = generate(70)
        self.assertEqual(len(doc['services']), 210)
        self.assertEqual(len(doc['volumes']), 70)
        ports = set()
        for i in range(1, 71):
            d = f'num{i:02d}'
            android = doc['services'][f'android-{d}']
            proxy = doc['services'][f'proxy-{d}']
            screen = doc['services'][f'screen-{d}']
            self.assertEqual(android['restart'], 'no')
            self.assertEqual(android['network_mode'], f'service:proxy-{d}')
            self.assertIn('androidboot.use_memfd=true', android['command'])
            self.assertIn('ro.product.brand=redroid', android['command'])
            self.assertEqual(proxy['environment']['CONTROL_CIDR'], network_plan(i)['control_subnet'])
            self.assertNotIn('coolify', proxy['networks'])
            self.assertTrue(doc['networks'][f'control-{d}']['internal'])
            self.assertEqual(screen['labels'][f'traefik.http.routers.farm-{d}.middlewares'],
                             f'farm-auth@file,farm-{d}-strip')
            self.assertIn(f'PathPrefix(`/d/{d}/`)', screen['labels'][f'traefik.http.routers.farm-{d}.rule'])
            for service in (android, proxy, screen):
                self.assertEqual(service['profiles'], ['manual'])
                self.assertNotIn('farm.phone', service['labels'])
            ports.update(proxy['ports'])
        self.assertEqual(len(ports), 70)
        self.assertIn('127.0.0.1:5620:5555', ports)

    def test_no_devices_start_on_plain_deploy(self):
        doc = generate()
        self.assertEqual([k for k, v in doc['services'].items() if not v.get('profiles')], [])

    def test_limit(self):
        report = dict(capacity=10, available_ram_gib=80, reserved_ram_gib=19.2,
                      disk_free_gib=300, disk_total_gib=1000, free_inode_ratio=.8,
                      load_1m=5, cpu_cores=72)
        assert_capacity([f'android-num{i:02d}' for i in range(1, 10)], 'num10', report)
        with self.assertRaises(RuntimeError):
            assert_capacity([f'android-num{i:02d}' for i in range(1, 11)], 'num11', report)
        with self.assertRaises(RuntimeError):
            assert_capacity(['android-num01'], 'num01', report)

    def test_future_device_and_invalid_count(self):
        self.assertIn('android-num71', generate(71)['services'])
        high = generate_one(8192)
        self.assertEqual(set(high['services']),
                         {'proxy-num8192', 'android-num8192', 'screen-num8192'})
        for count in (0, DEVICE_LIMIT + 1):
            with self.assertRaises(ValueError):
                generate(count)

    def test_proxy_has_no_direct_fallback_and_dns_is_proxied(self):
        secret = dict(type='socks', server='8.8.8.8', server_port=1080, username='u', password='p')
        config = module.make_config(secret)
        self.assertEqual(len(config['outbounds']), 1)
        self.assertEqual(config['route']['final'], 'residential')
        self.assertEqual(config['dns']['servers'][0]['detour'], 'residential')
        self.assertEqual(config['outbounds'][0]['network'], 'tcp')
        for invalid in ('127.0.0.1', '10.1.2.3', '203.0.113.10', 'proxy.example.com', '::1'):
            with self.assertRaises(ValueError):
                module.make_config(dict(secret, server=invalid))


if __name__ == '__main__':
    unittest.main()
