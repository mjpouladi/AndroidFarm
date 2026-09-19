import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops.resources import capacity, admission
from ops.provision import choose_record
from ops.account_policy import assert_not_held
from ops.identity import verify
from ops import inventory
from ops.compose_factory import canonical_device, single_instance
from ops.farmctl import _global_ipv4, validate_compose
from ops import app_installer


class OperationsTests(unittest.TestCase):
    def test_resource_capacity(self):
        self.assertEqual(capacity(72, 96)[0], 10)
        self.assertEqual(capacity(16, 32)[0], 2)
        self.assertEqual(capacity(2, 4)[0], 0)
        self.assertEqual(capacity(128, 512)[0], 10)

    def test_admission_fails_closed_under_pressure(self):
        report = dict(capacity=10, available_ram_gib=80, reserved_ram_gib=19.2,
                      disk_free_gib=300, disk_total_gib=1000, free_inode_ratio=.8,
                      load_1m=5, cpu_cores=72)
        admission(report, 9)
        with self.assertRaises(RuntimeError):
            admission(report, 10)
        for change in ({'available_ram_gib': 23}, {'disk_free_gib': 90}, {'free_inode_ratio': .01}, {'load_1m': 100}):
            with self.assertRaises(RuntimeError):
                admission(dict(report, **change), 1)

    def test_sequential_allocation_and_resume(self):
        first = choose_record([], 'phone1', 'proxy1', 'request1')
        self.assertEqual(first['id'], 'num01')
        self.assertIs(choose_record([first], 'phone1', 'proxy1', 'request1'), first)
        with self.assertRaises(RuntimeError):
            choose_record([first], 'phone2', 'proxy2', 'request2')
        first['phase'] = 'ready_for_operator'
        self.assertEqual(choose_record([first], 'phone2', 'proxy2', 'request2')['id'], 'num02')
        for phone, proxy, request in [('phone2', 'proxy1', 'request2'), ('phone1', 'proxy2', 'request2')]:
            with self.assertRaises(RuntimeError):
                choose_record([first], phone, proxy, request)

    def test_hold_blocks_until_manually_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'holds.json'
            assert_not_held('num01', path)
            path.write_text(json.dumps({'num01': {'reason': 'account-restriction'}}))
            with self.assertRaises(RuntimeError):
                assert_not_held('num01', path)
            assert_not_held('num02', path)

    def test_identity_drift_never_rewrites_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            baseline = directory / 'num01.json'
            baseline.write_text('{}')
            with patch('ops.identity.snapshot', return_value={'ro.serialno': 'changed'}):
                with self.assertRaises(RuntimeError):
                    verify('num01', directory)
            self.assertEqual(baseline.read_text(), '{}')

    def test_identity_baseline_missing_after_initial_creation_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = inventory.empty_inventory()
            record, _ = inventory.choose(state, 'phone-1', 'proxy-1', 'request-1')
            record.update(phase='identity_baselining', identity_baseline_created=True)
            inventory.save(root / 'inventory.json', state)
            with patch('ops.identity.snapshot', return_value={'ro.serialno': 'farm-num01'}):
                with self.assertRaises(RuntimeError):
                    verify('num01', root / 'identities')

    def test_versioned_inventory_is_monotonic_and_migrates_legacy(self):
        state = inventory.empty_inventory()
        first, created = inventory.choose(state, 'phone-1', 'proxy-1', 'request-1')
        self.assertTrue(created)
        self.assertEqual(first['id'], 'num01')
        first['phase'] = 'ready_for_operator'
        second, _ = inventory.choose(state, 'phone-2', 'proxy-2', 'request-2')
        self.assertEqual(second['id'], 'num02')
        del state['devices']['num01']
        second['phase'] = 'ready_for_operator'
        third, _ = inventory.choose(state, 'phone-3', 'proxy-3', 'request-3')
        self.assertEqual(third['id'], 'num03')
        legacy = inventory.normalize([dict(first, id='num01')])
        self.assertEqual(legacy['next_index'], 2)

    def test_inventory_rejects_duplicate_identity(self):
        state = {'schema_version': 2, 'next_index': 3, 'devices': {
            'num01': {'id': 'num01', 'phone_hash': 'same', 'proxy_hash': 'p1'},
            'num02': {'id': 'num02', 'phone_hash': 'same', 'proxy_hash': 'p2'}}}
        with self.assertRaises(RuntimeError):
            inventory.normalize(state)

    def test_honest_single_instance_profile_and_alias(self):
        self.assertEqual(canonical_device('dev01'), 'num01')
        document = single_instance('dev01', 'phone_fhd')
        android = document['services']['android-num01']
        self.assertTrue(android['volumes'][0]['source'].replace('\\', '/').endswith('/opt/farm/data/instances/num01/data'))
        self.assertIn('ro.product.brand=redroid', android['command'])
        self.assertIn('ro.product.model=Redroid QA Phone FHD', android['command'])
        self.assertFalse(any('samsung' in value.lower() for value in android['command']))

    def test_public_ip_parser_rejects_private_and_malformed(self):
        self.assertEqual(_global_ipv4('HTTP/1.0 200 OK\r\n\r\n8.8.8.8'), '8.8.8.8')
        for text in ('10.1.2.3', '999.1.2.3', 'empty'):
            with self.assertRaises(RuntimeError):
                _global_ipv4(text)

    def test_compose_policy_accepts_expected_shape_and_rejects_public_adb(self):
        config = {'services': {
            'proxy-num01': {
                'container_name': 'proxy-num01', 'restart': 'no',
                'cap_add': ['NET_ADMIN'], 'cap_drop': ['NET_RAW'],
                'networks': {'egress-num01': {'ipv4_address': '10.231.1.2'},
                             'control-num01': {'ipv4_address': '10.232.1.2'}},
                'secrets': [{'source': 'proxy-num01', 'target': 'proxy.json'}],
                'ports': [{'target': 5555, 'host_ip': '127.0.0.1', 'protocol': 'tcp', 'published': '5551'}]},
            'android-num01': {
                'container_name': 'android-num01', 'restart': 'no', 'privileged': True,
                'network_mode': 'service:proxy-num01',
                'volumes': [{'type': 'volume', 'source': 'redroid-data-num01', 'target': '/data'}],
                'command': ['androidboot.serialno=farm-num01', 'androidboot.use_memfd=true',
                            'ro.product.brand=redroid', 'ro.product.manufacturer=remote-android',
                            'ro.product.model=Redroid QA Phone']},
            'screen-num01': {
                'container_name': 'screen-num01', 'restart': 'no', 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'],
                'networks': {'coolify': {}, 'control-num01': {'ipv4_address': '10.232.1.3'}},
                'labels': {'traefik.enable': 'true',
                           'traefik.http.routers.farm-num01.middlewares': 'farm-auth@file,farm-num01-strip',
                           'traefik.http.routers.farm-num01.rule': 'Host(`farm.example.com`) && PathPrefix(`/d/num01/`)',
                           'traefik.http.services.farm-num01.loadbalancer.server.port': '6080'}},
            },
            'networks': {
                'egress-num01': {'name': 'farm-egress-num01', 'driver': 'bridge',
                                 'driver_opts': {'com.docker.network.bridge.name': 'br-af001'},
                                 'ipam': {'config': [{'subnet': '10.231.1.0/29'}]}},
                'control-num01': {'name': 'farm-control-num01', 'internal': True,
                                  'ipam': {'config': [{'subnet': '10.232.1.0/29'}]}},
                'coolify': {'external': True, 'name': 'coolify'}},
            'volumes': {'redroid-data-num01': {'external': True, 'name': 'redroid-data-num01'}},
            'secrets': {'proxy-num01': {'file': '/etc/android-farm/secrets/num01.json'}}}
        validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))
        config['services']['proxy-num01']['ports'][0]['host_ip'] = '0.0.0.0'
        with self.assertRaises(RuntimeError):
            validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))
        config['services']['proxy-num01']['ports'][0]['host_ip'] = '127.0.0.1'
        config['services']['proxy-num01']['networks']['side-channel'] = {}
        with self.assertRaises(RuntimeError):
            validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))

    def test_generic_apk_requires_separate_package_trust_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            apk = root / 'qa.apk'
            apk.write_bytes(b'approved artifact')
            apk.chmod(0o600)
            policy = root / 'trust.json'
            certificate = 'a' * 64
            policy.write_text(json.dumps({'packages': {'com.example.qaapp': {'signers': [certificate]}}}))
            policy.chmod(0o600)
            checksum = hashlib.sha256(apk.read_bytes()).hexdigest()
            with patch('ops.app_installer.output', side_effect=[
                    f'Signer #1 certificate SHA-256 digest: {certificate}',
                    "package: name='com.example.qaapp'\nsdkVersion:'23'\nnative-code: 'arm64-v8a'"]):
                artifact = app_installer.verify(apk, checksum, 'com.example.qaapp', policy)
            self.assertEqual(artifact['package'], 'com.example.qaapp')
            with patch('ops.app_installer.output', side_effect=[
                    'Signer #1 certificate SHA-256 digest: ' + 'b' * 64,
                    "package: name='com.example.qaapp'"]):
                with self.assertRaises(RuntimeError):
                    app_installer.verify(apk, checksum, 'com.example.qaapp', policy)
            with self.assertRaises(ValueError):
                app_installer.install('num01', artifact, ['android.permission.READ_SMS'])


if __name__ == '__main__':
    unittest.main()
