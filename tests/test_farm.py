import importlib.util
import json
import unittest
from pathlib import Path
from generate_farm import generate
from ops.farmctl import assert_capacity

spec = importlib.util.spec_from_file_location('proxy_config', Path('images/proxy/configure.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FarmTests(unittest.TestCase):
    def test_delivered_files_match_generator(self):
        for count, path in ((1, 'docker-compose.yml'), (70, 'docker-compose.farm.yml')):
            self.assertEqual(json.loads(Path(path).read_text()), generate(count))

    def test_scale_and_network_isolation(self):
        doc = generate(70)
        self.assertEqual(len(doc['services']), 211)
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
            self.assertEqual(proxy['environment']['CONTROL_CIDR'], f'10.232.{i}.0/29')
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
        self.assertEqual([k for k, v in doc['services'].items() if not v.get('profiles')], ['farm-anchor'])

    def test_limit(self):
        assert_capacity([f'android-num{i:02d}' for i in range(1, 10)], 'num10')
        with self.assertRaises(RuntimeError):
            assert_capacity([f'android-num{i:02d}' for i in range(1, 11)], 'num11')
        with self.assertRaises(RuntimeError):
            assert_capacity(['android-num01'], 'num01')

    def test_future_device_and_invalid_count(self):
        self.assertIn('android-num71', generate(71)['services'])
        for count in (0, 201):
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
