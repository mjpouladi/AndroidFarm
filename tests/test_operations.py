import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops.resources import capacity, admission
from ops.provision import choose_record
from ops.account_policy import assert_not_held
from ops.identity import verify
from ops import identity
from ops.device_ids import network_plan
from ops.device_profiles import apply_profile, validate as validate_device_profile
from ops import inventory
from ops.compose_factory import canonical_device, single_instance
from ops.farmctl import (_global_ipv4, recover_existing_screen,
                         recovery_android_is_active, validate_compose)
from ops import app_installer
from provisioner import (Config, bulk_managed_inspections, farmctl as provisioner_farmctl,
                         validate_console_origin)


class OperationsTests(unittest.TestCase):
    def test_console_origin_requires_explicit_safe_ip_mode_for_http(self):
        self.assertEqual(validate_console_origin("https://farm.example.com/"),
                         "https://farm.example.com")
        self.assertEqual(validate_console_origin("http://10.20.30.40:18080", "ip"),
                         "http://10.20.30.40:18080")
        self.assertEqual(validate_console_origin("http://192.168.10.4:18080/", "ip"),
                         "http://192.168.10.4:18080")
        self.assertEqual(Config(Path("compose"), None, "project", Path("secrets"),
                                Path("backups"), "https://farm.example.com").access_mode,
                         "domain")
        invalid = (
            ("http://farm.example.com:18080", "domain"),
            ("http://farm.example.com:18080", "ip"),
            ("http://127.0.0.1:18080", "ip"),
            ("http://10.20.30.40:8000", "ip"),
            ("http://user:pass@10.20.30.40:18080", "ip"),
            ("http://10.20.30.40:18080/path", "ip"),
            ("http://10.20.30.40:18080?query=1", "ip"),
            ("http://10.20.30.40:18080?", "ip"),
            ("https://farm.example.com#", "domain"),
            ("https://farm.example.com evil", "domain"),
            ("https://10.20.30.40:18080", "ip"),
        )
        for origin, mode in invalid:
            with self.subTest(origin=origin, mode=mode), self.assertRaises(RuntimeError):
                validate_console_origin(origin, mode)

    def test_status_bulk_inspection_scales_with_materialized_containers(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            if argv[1:3] == ['ps', '-a']:
                return subprocess.CompletedProcess(
                    argv, 0, 'proxy-num01\nandroid-num01\nscreen-num01\nnot-managed\n', '')
            items = [{'Name': '/' + name, 'State': {'Running': True}} for name in argv[2:]]
            return subprocess.CompletedProcess(argv, 0, json.dumps(items), '')

        result = bulk_managed_inspections(['num01', 'num02'], runner=runner)
        self.assertEqual(set(result), {'proxy-num01', 'android-num01', 'screen-num01'})
        self.assertEqual(len(calls), 2)
        self.assertNotIn('not-managed', calls[1])

    def test_resource_capacity(self):
        self.assertEqual(capacity(72, 96)[0], 10)
        self.assertEqual(capacity(16, 32)[0], 2)
        self.assertEqual(capacity(2, 4)[0], 0)
        self.assertEqual(capacity(128, 512)[0], 19)

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
        split_storage = dict(report, data_disk_total_gib=1000, data_disk_free_gib=300,
                             data_free_inode_ratio=.8, docker_disk_total_gib=200,
                             docker_disk_free_gib=100, docker_free_inode_ratio=.8)
        admission(split_storage, 1)
        with self.assertRaisesRegex(RuntimeError, 'Docker root storage'):
            admission(dict(split_storage, docker_disk_free_gib=10), 1)
        with self.assertRaisesRegex(RuntimeError, 'Docker root storage'):
            admission(dict(split_storage, docker_free_inode_ratio=.01), 1)

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
            path.chmod(0o600)  # Private state must be 0600 whatever the caller's umask is.
            with self.assertRaises(RuntimeError):
                assert_not_held('num01', path)
            assert_not_held('num02', path)

    def test_identity_drift_never_rewrites_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            baseline = directory / 'num01.json'
            baseline.write_text('{}')
            baseline.chmod(0o600)
            with patch('ops.identity.snapshot', return_value={'ro.serialno': 'changed'}):
                with self.assertRaisesRegex(RuntimeError, 'identity drift'):
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

    def test_identity_probe_uses_shared_allocator_beyond_first_subnet(self):
        commands = []

        def output(command, **_kwargs):
            commands.append(command)
            if command[-2:] == ['getprop', 'sys.boot_completed']:
                return '1\n'
            if command[-2:] == ['getprop', 'ro.serialno']:
                return 'farm-num129\n'
            if command[-3:] == ['settings', 'get', 'secure']:
                return 'unused\n'
            if command[-4:] == ['settings', 'get', 'secure', 'android_id']:
                return '0123456789abcdef\n'
            return 'qa-value\n'

        with patch('ops.identity.subprocess.check_output', side_effect=output):
            identity.snapshot('num129', timeout=1)
        target = f"{network_plan(129)['proxy_control_ip']}:5555"
        self.assertTrue(commands)
        self.assertTrue(all(command[5] == target for command in commands))

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
        self.assertEqual(android['volumes'], ['redroid-data-num01:/data'])
        self.assertEqual(document['volumes']['redroid-data-num01'],
                         {'external': True, 'name': 'redroid-data-num01'})
        self.assertIn('ro.product.brand=redroid', android['command'])
        self.assertIn('ro.product.model=Redroid QA Phone FHD', android['command'])
        self.assertFalse(any('samsung' in value.lower() for value in android['command']))
        self.assertEqual(document['services']['screen-num01']['environment']['SCREEN_WIDTH'], '1080')

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
                'networks': {'egress-num01': {'ipv4_address': '10.231.0.2'},
                             'control-num01': {'ipv4_address': '10.232.0.2'}},
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
                'environment': {'ADB_TARGET': '10.232.0.2:5555', 'SCREEN_WIDTH': '720',
                                'SCREEN_HEIGHT': '1280', 'SCREEN_FPS': '20'},
                'networks': {'coolify': {}, 'control-num01': {'ipv4_address': '10.232.0.3'}},
                'labels': {'traefik.enable': 'true',
                           'traefik.http.routers.farm-num01.middlewares': 'farm-auth@file,farm-num01-strip',
                           'traefik.http.routers.farm-num01.rule': 'Host(`farm.example.com`) && PathPrefix(`/d/num01/`)',
                           'traefik.http.services.farm-num01.loadbalancer.server.port': '6080'}},
            },
            'networks': {
                'egress-num01': {'name': 'farm-egress-num01', 'driver': 'bridge',
                                 'driver_opts': {'com.docker.network.bridge.name': 'br-af00001'},
                                 'ipam': {'config': [{'subnet': '10.231.0.0/29'}]}},
                'control-num01': {'name': 'farm-control-num01', 'internal': True,
                                  'ipam': {'config': [{'subnet': '10.232.0.0/29'}]}},
                'coolify': {'external': True, 'name': 'coolify'}},
            'volumes': {'redroid-data-num01': {'external': True, 'name': 'redroid-data-num01'}},
            'secrets': {'proxy-num01': {'file': '/etc/android-farm/secrets/num01.json'}}}
        validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))
        labels = config['services']['screen-num01']['labels']
        labels['traefik.enable'] = 'false'
        validate_compose(config, 'num01', Path('/etc/android-farm/secrets'), access_mode='ip')
        with self.assertRaisesRegex(RuntimeError, 'access mode'):
            validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))
        middleware = labels['traefik.http.routers.farm-num01.middlewares']
        labels['traefik.http.routers.farm-num01.middlewares'] = 'farm-num01-strip'
        with self.assertRaisesRegex(RuntimeError, 'auth middleware'):
            validate_compose(config, 'num01', Path('/etc/android-farm/secrets'), access_mode='ip')
        labels['traefik.http.routers.farm-num01.middlewares'] = middleware
        labels['traefik.enable'] = 'true'
        profile = validate_device_profile({
            'schema_version': 1, 'android_version': 12,
            'resolution': {'width': 1080, 'height': 1920}, 'dpi': 420, 'fps': 30,
            'device_model': 'Android Farm QA Phone FHD', 'locale': 'fa-IR',
        })
        profiled = apply_profile(config, 'num01', profile)
        validate_compose(profiled, 'num01', Path('/etc/android-farm/secrets'), profile)
        ceiling = validate_device_profile({
            'schema_version': 2, 'android_version': 13,
            'resolution': {'width': 720, 'height': 1280}, 'dpi': 240, 'fps': 20,
            'device_model': 'Android Farm QA Phone HD', 'locale': 'en-US',
            'timezone': 'Asia/Tehran', 'resources': {'cpus': 2, 'memory_gib': 3},
        })
        limited = apply_profile(config, 'num01', ceiling)
        validate_compose(limited, 'num01', Path('/etc/android-farm/secrets'), ceiling)
        # `docker compose config` renders the limit in bytes and cpus as a number.
        limited['services']['android-num01'].update(mem_limit=str(3 * 1024 ** 3), cpus=2)
        validate_compose(limited, 'num01', Path('/etc/android-farm/secrets'), ceiling)
        for drift in ({'cpus': 4}, {'mem_limit': '4g'}, {'cpus': None}):
            drifted = apply_profile(config, 'num01', ceiling)
            drifted['services']['android-num01'].update(drift)
            with self.subTest(drift=drift), self.assertRaisesRegex(RuntimeError, 'resource ceiling'):
                validate_compose(drifted, 'num01', Path('/etc/android-farm/secrets'), ceiling)
        config['services']['proxy-num01']['ports'][0]['host_ip'] = '0.0.0.0'
        with self.assertRaises(RuntimeError):
            validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))
        config['services']['proxy-num01']['ports'][0]['host_ip'] = '127.0.0.1'
        config['services']['proxy-num01']['ports'].append(
            {'target': 8080, 'host_ip': '0.0.0.0', 'protocol': 'tcp', 'published': '8080'})
        with self.assertRaisesRegex(RuntimeError, 'only published port'):
            validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))
        config['services']['proxy-num01']['ports'].pop()
        config['services']['proxy-num01']['networks']['side-channel'] = {}
        with self.assertRaises(RuntimeError):
            validate_compose(config, 'num01', Path('/etc/android-farm/secrets'))

    def test_direct_egress_guard_passes_through_when_open_and_drops_when_closed(self):
        from ops import farmctl
        payloads = []

        def fake_subprocess(argv, **kwargs):
            if argv[:2] == ['iptables-restore', '-w']:
                payloads.append(kwargs['input'])
            # The chain and the DOCKER-USER jump already exist in this fixture.
            return subprocess.CompletedProcess(argv, 0, '', '')

        secrets = [{'type': 'direct'}, {'type': 'direct'},
                   {'type': 'socks', 'server': '8.8.8.8', 'server_port': 1080, 'username': 'u', 'password': 'p'}]
        with patch('ops.farmctl.read_private_json', side_effect=secrets), patch('ops.farmctl.run'), \
                patch('ops.farmctl.subprocess.run', side_effect=fake_subprocess):
            farmctl.guard('num01', Path('/etc/android-farm/secrets'), allow_upstream=True)
            farmctl.guard('num01', Path('/etc/android-farm/secrets'), allow_upstream=False)
            farmctl.guard('num01', Path('/etc/android-farm/secrets'), allow_upstream=True)
        self.assertEqual(payloads[0].splitlines(), ['*filter', '-F AF00001', '-A AF00001 -j RETURN',
                                                    '-A AF00001 -j DROP', 'COMMIT'])
        self.assertEqual(payloads[1].splitlines(), ['*filter', '-F AF00001', '-A AF00001 -j DROP', 'COMMIT'])
        self.assertEqual(payloads[2].splitlines(), ['*filter', '-F AF00001',
                                                    '-A AF00001 -p tcp -d 8.8.8.8 --dport 1080 -j RETURN',
                                                    '-A AF00001 -j DROP', 'COMMIT'])
        with patch('ops.farmctl.read_private_json', return_value={'type': 'direct', 'server': '8.8.8.8'}), \
                patch('ops.farmctl.run'), patch('ops.farmctl.subprocess.run', side_effect=fake_subprocess), \
                self.assertRaisesRegex(RuntimeError, 'unexpected upstream'):
            farmctl.guard('num01', Path('/etc/android-farm/secrets'))
        self.assertEqual(len(payloads), 3)

    def test_crash_recovery_needs_an_abnormal_exit_and_a_running_intent(self):
        from ops import farmctl
        crashed = {'State': {'Running': False, 'ExitCode': 139}}
        oom = {'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': True}}
        clean = {'State': {'Running': False, 'ExitCode': 0}}
        running = {'State': {'Running': True, 'ExitCode': 0}}
        for item, wanted, expected in ((crashed, True, True), (oom, True, True), (crashed, False, False),
                                       (clean, True, False), (running, True, False), (None, True, False)):
            with self.subTest(item=item, wanted=wanted), \
                    patch('ops.farmctl.inspect', return_value=item), \
                    patch('ops.farmctl.desired_state.wants_running', return_value=wanted):
                self.assertEqual(farmctl.crashed_and_wanted('num01'), expected)

    def test_direct_device_validation_requires_the_exact_direct_secret(self):
        from ops import farmctl
        record = {'id': 'num01', 'egress': 'direct'}
        with patch('ops.farmctl.read_private_json', return_value={'type': 'direct'}):
            farmctl.validate_managed_proxy('num01', record, Path('/etc/android-farm/secrets'))
        for installed, current in (({'type': 'direct'}, dict(record, proxy_id='qa-proxy')),
                                   ({'type': 'socks', 'server': '8.8.8.8'}, record)):
            with self.subTest(installed=installed, record=current), \
                    patch('ops.farmctl.read_private_json', return_value=installed), \
                    self.assertRaisesRegex(RuntimeError, 'direct egress'):
                farmctl.validate_managed_proxy('num01', current, Path('/etc/android-farm/secrets'))

    def test_provisioner_propagates_ip_access_mode_to_guarded_lifecycle(self):
        config = Config(Path('compose'), Path('env'), 'project', Path('secrets'),
                        Path('backups'), 'http://192.168.10.4:18080', access_mode='ip')
        with patch('provisioner.invoke') as invoke:
            provisioner_farmctl(config, 'start', 'num01')
        argv = invoke.call_args.args
        self.assertEqual(argv[:4], ('farmctl.py', 'start', 'num01', '--compose'))
        self.assertEqual(argv[argv.index('--access-mode') + 1], 'ip')

    def test_health_recovery_rechecks_on_demand_state_before_start(self):
        stopped = {'State': {'Running': False}}
        with patch('ops.farmctl.inspect', return_value=stopped):
            self.assertFalse(recovery_android_is_active('num01'))
        with patch('ops.farmctl.inspect', side_effect=[stopped, {'State': {'Running': True}}, stopped]), \
                patch('ops.farmctl.run') as command:
            self.assertFalse(recover_existing_screen('num01'))
            command.assert_not_called()

    def test_screen_recovery_starts_only_existing_container_behind_healthy_proxy(self):
        running = {'State': {'Running': True}}
        stopped = {'State': {'Running': False}}
        with patch('ops.farmctl.inspect', side_effect=[running, running, stopped]), \
                patch('ops.farmctl.assert_not_held') as hold, \
                patch('ops.farmctl.run') as command:
            self.assertTrue(recover_existing_screen('num01'))
        hold.assert_called_once_with('num01')
        self.assertEqual(command.call_args_list[0].args,
                         ('docker', 'exec', 'proxy-num01', '/healthcheck.sh'))
        self.assertEqual(command.call_args_list[1].args,
                         ('docker', 'start', 'screen-num01'))

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
