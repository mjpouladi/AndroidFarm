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

    def test_monitoring_chain_is_internal_alerts_are_routed_and_logs_are_shipped(self):
        import yaml
        compose = yaml.safe_load(Path('docker-compose.yml').read_text(encoding='utf-8'))
        services = compose['services']
        for name in ('alertmanager', 'loki', 'promtail'):
            with self.subTest(service=name):
                service = services[name]
                self.assertEqual(service['networks'], ['monitoring'])
                self.assertNotIn('ports', service)
                self.assertTrue(service['read_only'])
                self.assertEqual(service['cap_drop'], ['ALL'])
                self.assertIn('farm.stack=core', service['labels'])
                self.assertIn('traefik.enable=false', service['labels'])
        self.assertTrue(compose['networks']['monitoring']['internal'])
        self.assertIn('/var/run/docker.sock:/var/run/docker.sock:ro', services['promtail']['volumes'])
        self.assertFalse(any('docker.sock' in volume for volume in services['loki']['volumes']))
        prometheus = yaml.safe_load(Path('monitoring/prometheus.yml').read_text(encoding='utf-8'))
        self.assertEqual(prometheus['alerting']['alertmanagers'][0]['static_configs'][0]['targets'], ['alertmanager:9093'])
        alertmanager = yaml.safe_load(Path('monitoring/alertmanager.yml').read_text(encoding='utf-8'))
        self.assertEqual(alertmanager['route']['receiver'], alertmanager['receivers'][0]['name'])
        loki = yaml.safe_load(Path('monitoring/loki.yml').read_text(encoding='utf-8'))
        self.assertFalse(loki['auth_enabled'])
        self.assertTrue(loki['compactor']['retention_enabled'])
        self.assertEqual(loki['limits_config']['retention_period'], '168h')
        promtail = yaml.safe_load(Path('monitoring/promtail.yml').read_text(encoding='utf-8'))
        scrape = promtail['scrape_configs'][0]
        self.assertEqual(scrape['docker_sd_configs'][0]['filters'], [{'name': 'label', 'values': ['farm.stack']}])
        self.assertIn('device', [rule['target_label'] for rule in scrape['relabel_configs']])
        self.assertTrue(any('REDACTED' in json.dumps(stage) for stage in scrape['pipeline_stages']))
        datasources = yaml.safe_load(Path('monitoring/grafana/provisioning/datasources/prometheus.yml').read_text(encoding='utf-8'))
        self.assertEqual({item['type'] for item in datasources['datasources']}, {'prometheus', 'alertmanager', 'loki'})
        dashboard = json.loads(Path('monitoring/grafana/dashboards/android-farm.json').read_text(encoding='utf-8'))
        logs = next(panel for panel in dashboard['panels'] if panel['type'] == 'logs')
        self.assertEqual(logs['datasource']['uid'], 'android-farm-loki')
        alerts = yaml.safe_load(Path('monitoring/alerts.yml').read_text(encoding='utf-8'))
        names = [rule['alert'] for group in alerts['groups'] for rule in group['rules']]
        self.assertIn('AndroidFarmContainerCrashed', names)
        from installer.quickstart import CORE_CONTAINERS
        from services.api.components import CONTAINERS
        for name in ('android-farm-alertmanager', 'android-farm-loki', 'android-farm-promtail'):
            self.assertIn(name, CORE_CONTAINERS)
            self.assertIn(name, CONTAINERS)

    def test_direct_egress_is_an_explicit_secret_not_a_fallback(self):
        # Only the exact {"type": "direct"} secret selects the tunnel-free mode.
        self.assertIsNone(module.make_config({'type': 'direct'}))
        for smuggled in ({'type': 'direct', 'server': '8.8.8.8'},
                         {'type': 'direct', 'username': 'u', 'password': 'p'}):
            with self.subTest(secret=smuggled), self.assertRaises(ValueError):
                module.make_config(smuggled)
        entrypoint = Path('images/proxy/entrypoint.sh').read_text(encoding='utf-8')
        # The closed policy is installed first, a stale marker is removed before
        # the secret is parsed, and direct mode is entered only through it.
        self.assertLess(entrypoint.index('iptables -P OUTPUT DROP'), entrypoint.index('python3 /configure.py'))
        self.assertLess(entrypoint.index('rm -f /run/direct'), entrypoint.index('python3 /configure.py'))
        self.assertIn('if [ -f /run/direct ]; then', entrypoint)
        direct_block = entrypoint.split('if [ -f /run/direct ]; then', 1)[1].split('fi', 1)[0]
        self.assertIn('--dport 5555 -j ACCEPT', direct_block)
        self.assertNotIn('sing-box', direct_block)
        self.assertNotIn('REDIRECT', direct_block)
        self.assertIn('exec ', direct_block)


if __name__ == '__main__':
    unittest.main()
