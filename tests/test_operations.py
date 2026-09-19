import hashlib
import json
from pathlib import Path
import subprocess
import sys
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

    def test_remove_decommissions_a_device_and_never_reuses_its_identifier(self):
        import os
        from unittest.mock import Mock
        from ops import farmctl
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            os.environ['ANDROID_FARM_EVENT_LOG'] = str(root / 'events.jsonl')
            self.addCleanup(os.environ.pop, 'ANDROID_FARM_EVENT_LOG', None)
            state = inventory.empty_inventory()
            record, _ = inventory.choose(state, 'phone-1', 'proxy-1', 'request-1')
            record.update(phase='failed', proxy_id='qa-proxy')
            inventory.save(root / 'inventory.json', state)
            for name, payload in (('holds.json', {'num01': {'reason': 'maintenance', 'at': 1}}),
                                  ('desired-state.json', {'schema_version': 1, 'devices': {'num01': {'running': True, 'updated_at': 1}}})):
                (root / name).write_text(json.dumps(payload))
                (root / name).chmod(0o600)
            files = {'secret': root / 'secrets' / 'num01.json', 'override': root / 'device-overrides' / 'num01.json',
                     'identity': root / 'identities' / 'num01.json', 'profile': root / 'profiles' / 'num01.json'}
            for path in files.values():
                path.parent.mkdir(mode=0o700, exist_ok=True)
                path.write_text('{}')
                path.chmod(0o600)
            data_root = root / 'instances'
            (data_root / 'num01' / 'data').mkdir(parents=True)
            commands = []

            def fake_subprocess(argv, **kwargs):
                commands.append(list(argv))
                missing = argv[:3] == ['docker', 'network', 'inspect'] and argv[3] == 'farm-control-num01'
                return subprocess.CompletedProcess(argv, 1 if missing else 0, '', '')

            def fake_inspect(name):
                return {'State': {'Running': False}} if name != 'proxy-num01' else None

            store = Mock()
            with patch('ops.farmctl.stop', side_effect=RuntimeError('host egress guard update failed')) as stop, \
                    patch('ops.farmctl.inspect', side_effect=fake_inspect), \
                    patch('ops.farmctl.run', side_effect=lambda *argv, **kw: commands.append(list(argv))), \
                    patch('ops.farmctl.subprocess.run', side_effect=fake_subprocess), \
                    patch('ops.farmctl.ProxyStore', return_value=store), \
                    patch('ops.farmctl.DATA_ROOT', data_root):
                removed = farmctl.remove('dev01', root / 'secrets', root / 'proxies.json', root / 'proxies',
                                         state_dir=root, profile_dir=root / 'profiles')
            stop.assert_called_once_with('num01')
            self.assertEqual(removed['containers'], ['screen-num01', 'android-num01'])
            self.assertEqual(removed['networks'], ['farm-egress-num01'])
            self.assertIsNone(removed['volume'])
            self.assertIsNone(removed['data_directory'])
            self.assertEqual(removed['proxy_released'], 'qa-proxy')
            store.unassign.assert_called_once_with('qa-proxy', 'num01')
            self.assertIn(['docker', 'rm', '-f', 'android-num01'], commands)
            self.assertIn(['iptables', '-w', '-X', 'AF00001'], commands)
            self.assertIn(['docker', 'network', 'rm', 'farm-egress-num01'], commands)
            self.assertNotIn(['docker', 'network', 'rm', 'farm-control-num01'], commands)
            self.assertFalse(any(argv[:3] == ['docker', 'volume', 'rm'] for argv in commands))
            # Secrets and overrides are gone; the identity baseline, profile and data survive without --purge-data.
            self.assertFalse(files['secret'].exists())
            self.assertFalse(files['override'].exists())
            self.assertTrue(files['identity'].exists() and files['profile'].exists())
            self.assertTrue((data_root / 'num01' / 'data').exists())
            self.assertEqual(json.loads((root / 'holds.json').read_text()), {})
            self.assertEqual(json.loads((root / 'desired-state.json').read_text())['devices'], {})
            after = inventory.load(root / 'inventory.json')
            self.assertEqual(after['devices'], {})
            self.assertEqual(after['next_index'], 2)
            self.assertEqual(inventory.choose(after, 'phone-2', 'proxy-2', 'request-2')[0]['id'], 'num02')
            self.assertIn('"kind":"device-removed"', (root / 'events.jsonl').read_text())
            with self.assertRaisesRegex(RuntimeError, 'not allocated'):
                with patch('ops.farmctl.stop'):
                    farmctl.remove('num01', root / 'secrets', root / 'proxies.json', root / 'proxies', state_dir=root)

    def test_remove_with_purge_drops_the_volume_data_identity_and_profile(self):
        import os
        from unittest.mock import Mock
        from ops import farmctl
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            os.environ['ANDROID_FARM_EVENT_LOG'] = str(root / 'events.jsonl')
            self.addCleanup(os.environ.pop, 'ANDROID_FARM_EVENT_LOG', None)
            state = inventory.empty_inventory()
            record, _ = inventory.choose(state, 'phone-1', 'direct:phone-1', 'request-1')
            record.update(phase='ready_for_operator', egress='direct')
            inventory.save(root / 'inventory.json', state)
            for path in (root / 'identities' / 'num01.json', root / 'profiles' / 'num01.json'):
                path.parent.mkdir(mode=0o700, exist_ok=True)
                path.write_text('{}')
                path.chmod(0o600)
            data_root = root / 'instances'
            (data_root / 'num01' / 'data').mkdir(parents=True)
            (data_root / 'num01' / 'data' / 'file').write_text('x')
            commands = []
            with patch('ops.farmctl.stop'), patch('ops.farmctl.inspect', return_value=None), \
                    patch('ops.farmctl.run', side_effect=lambda *argv, **kw: commands.append(list(argv))), \
                    patch('ops.farmctl.subprocess.run',
                          side_effect=lambda argv, **kw: (commands.append(list(argv)) or subprocess.CompletedProcess(argv, 0, '', ''))), \
                    patch('ops.farmctl.ProxyStore', return_value=Mock()) as store, \
                    patch('ops.farmctl.DATA_ROOT', data_root):
                removed = farmctl.remove('num01', root / 'secrets', root / 'proxies.json', root / 'proxies',
                                         purge_data=True, state_dir=root, profile_dir=root / 'profiles')
            self.assertEqual(removed['volume'], 'redroid-data-num01')
            self.assertEqual(removed['data_directory'], str(data_root / 'num01'))
            self.assertIsNone(removed['proxy_released'])
            store.assert_not_called()
            self.assertIn(['docker', 'volume', 'rm', 'redroid-data-num01'], commands)
            self.assertFalse((data_root / 'num01').exists())
            self.assertFalse((root / 'identities' / 'num01.json').exists())
            self.assertFalse((root / 'profiles' / 'num01.json').exists())
            self.assertEqual(inventory.load(root / 'inventory.json')['devices'], {})

    def test_provisioner_remove_forwards_only_the_explicit_purge_flag(self):
        import provisioner
        config = Config(Path('compose'), None, 'project', Path('secrets'), Path('backups'), 'https://farm.example.com')
        with patch('provisioner.load_config', return_value=config), patch('provisioner.invoke') as invoke, \
                patch('sys.argv', ['device-provisioner', '--config', 'config.json', 'remove', '--id', 'dev01']):
            provisioner.main()
        self.assertEqual(invoke.call_args.args[:3], ('farmctl.py', 'remove', 'num01'))
        self.assertNotIn('--purge-data', invoke.call_args.args)
        with patch('provisioner.load_config', return_value=config), patch('provisioner.invoke') as invoke, \
                patch('sys.argv', ['device-provisioner', '--config', 'config.json', 'remove', '--id', 'num01', '--purge-data']):
            provisioner.main()
        self.assertEqual(invoke.call_args.args[-1], '--purge-data')

    def test_status_report_tolerates_a_device_without_containers_and_names_the_reason(self):
        import io
        from contextlib import redirect_stdout
        import provisioner
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = inventory.empty_inventory()
            record, _ = inventory.choose(state, 'phone-1', 'direct:phone-1', 'request-1')
            record.update(phase='failed', egress='direct', last_error='guarded start failed: identity check: Android boot timed out',
                          failed_at=1700000000)
            inventory.save(root / 'inventory.json', state)
            config = Config(root / 'compose.yml', None, 'farm', root / 'secrets', root / 'backups',
                            'https://farm.example.com', state_dir=root)
            with patch('provisioner.bulk_managed_inspections', return_value={}), \
                    patch('provisioner.resources.probe', return_value={'capacity': 1}), \
                    patch('provisioner.subprocess.run') as stats:
                payload = provisioner.collect_status(config)
            stats.assert_not_called()
            row = payload['devices'][0]
            self.assertEqual(row['containers'], {'proxy': 'missing', 'android': 'missing', 'screen': 'missing'})
            self.assertIsNone(row['adb'])
            self.assertEqual(row['last_error'], record['last_error'])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                provisioner.print_status(payload)
            self.assertIn('num01: incomplete preparation - guarded start failed', buffer.getvalue())

    def test_host_step_failures_are_described_without_their_argv(self):
        from provisioner import describe_failure
        failed = subprocess.CalledProcessError(1, [sys.executable, '-m', 'ops.provision', '--request', '/secret/path.json'])
        self.assertEqual(describe_failure(failed), 'host step ops.provision exited with status 1; its messages are printed above')
        self.assertNotIn('/secret/path.json', describe_failure(failed))
        docker = subprocess.CalledProcessError(2, ['docker', 'exec', 'screen-num01', 'adb'])
        self.assertIn('host step docker exited with status 2', describe_failure(docker))
        self.assertEqual(describe_failure(subprocess.TimeoutExpired(['docker', 'pull'], 300)),
                         'host step timed out after 300 seconds; retry when the host is idle')
        self.assertEqual(describe_failure(RuntimeError('plain')), 'plain')

    def test_invoke_streams_stderr_and_captures_only_stdout(self):
        import provisioner
        with patch('provisioner.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'num01: ok\n', None)) as run:
            provisioner.invoke('provision.py', '--request', 'r.json', capture=True)
            self.assertEqual(run.call_args.kwargs['stdout'], subprocess.PIPE)
            self.assertNotIn('stderr', run.call_args.kwargs)
            self.assertNotIn('capture_output', run.call_args.kwargs)
            provisioner.invoke('account_policy.py', 'hold', 'num01', '--reason', 'maintenance')
            self.assertIsNone(run.call_args.kwargs['stdout'])

    def test_android_egress_probe_retries_transient_adb_failures(self):
        from ops import farmctl
        offline = subprocess.CalledProcessError(1, ['adb'], 'error: device offline')
        with patch('ops.farmctl.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                patch('ops.farmctl.run', side_effect=[offline, '', '8.8.8.8\n']) as probe:
            self.assertEqual(farmctl.android_egress_ip('num01', sleep=lambda _: None), '8.8.8.8')
        self.assertEqual(probe.call_count, 3)
        with patch('ops.farmctl.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                patch('ops.farmctl.run', side_effect=offline), \
                self.assertRaisesRegex(RuntimeError, 'ADB did not answer after the unpause'):
            farmctl.android_egress_ip('num01', attempts=3, sleep=lambda _: None)
        with patch('ops.farmctl.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                patch('ops.farmctl.run', return_value='10.0.0.1'), \
                self.assertRaisesRegex(RuntimeError, 'egress probe did not return'):
            farmctl.android_egress_ip('num01', attempts=2, sleep=lambda _: None)

    def test_recovery_leaves_a_device_alone_during_preparation(self):
        from ops import farmctl
        for phase in ('reserved', 'secret_installed', 'identity_baselining', 'starting', 'installing_apk', 'failed'):
            for action in ('recover', 'recover-crashed', 'recover-screen'):
                with self.subTest(phase=phase, action=action):
                    self.assertEqual(farmctl.recovery_skip_reason(action, {'phase': phase}, 'num01'),
                                     'preparation-in-progress')
        self.assertEqual(farmctl.recovery_skip_reason('recover', None, 'num01'), 'not-allocated')
        self.assertIsNone(farmctl.recovery_skip_reason('start', {'phase': 'reserved'}, 'num01'))
        ready = {'phase': 'ready_for_operator'}
        with patch('ops.farmctl.android_answers', return_value=True):
            self.assertEqual(farmctl.recovery_skip_reason('recover', ready, 'num01'), 'healthy-now')
            self.assertIsNone(farmctl.recovery_skip_reason('recover-crashed', ready, 'num01'))
            self.assertIsNone(farmctl.recovery_skip_reason('recover-screen', ready, 'num01'))
        with patch('ops.farmctl.android_answers', return_value=False):
            self.assertIsNone(farmctl.recovery_skip_reason('recover', ready, 'num01'))
        answered = subprocess.CompletedProcess([], 0, '1\n', '')
        with patch('ops.farmctl.subprocess.run', return_value=answered):
            self.assertTrue(farmctl.android_answers('num01'))
        with patch('ops.farmctl.subprocess.run', side_effect=subprocess.TimeoutExpired(['adb'], 15)):
            self.assertFalse(farmctl.android_answers('num01'))

    def test_android_image_is_pulled_only_when_missing(self):
        from ops import farmctl
        resolved = {'services': {'android-num01': {'image': 'redroid/redroid:12.0.0-latest'}}}
        with patch('ops.farmctl.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                patch('ops.farmctl.run') as run:
            self.assertFalse(farmctl.ensure_android_image(resolved, 'num01'))
            run.assert_not_called()
        with patch('ops.farmctl.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                patch('ops.farmctl.run') as run:
            self.assertTrue(farmctl.ensure_android_image(resolved, 'num01'))
        self.assertEqual(run.call_args.args, ('docker', 'pull', '--quiet', 'redroid/redroid:12.0.0-latest'))
        self.assertEqual(run.call_args.kwargs['timeout'], 1800)
        with patch('ops.farmctl.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                patch('ops.farmctl.run', side_effect=subprocess.TimeoutExpired(['docker'], 1800)), \
                self.assertRaisesRegex(RuntimeError, 'Android image pull timed out'):
            farmctl.ensure_android_image(resolved, 'num01')

    def test_preparation_failure_reasons_are_short_and_secret_free(self):
        from ops.provision import failure_reason
        self.assertEqual(failure_reason('guarded start', RuntimeError('identity baseline was not committed by the guarded start')),
                         'guarded start failed: identity baseline was not committed by the guarded start')
        self.assertEqual(failure_reason('application installation', subprocess.CalledProcessError(1, ['adb', 'install', '/secret'])),
                         'application installation failed; the host job log of this request has the command output')
        self.assertEqual(failure_reason('egress verification', subprocess.TimeoutExpired(['curl', 'token=abc'], 25)),
                         'egress verification timed out')
        self.assertEqual(failure_reason('guarded start', KeyboardInterrupt()), 'guarded start interrupted')
        self.assertEqual(failure_reason('guarded start', OSError(13, 'Permission denied', '/etc/secret')),
                         'guarded start failed: Permission denied')
        self.assertLessEqual(len(failure_reason('guarded start', RuntimeError('x' * 500))), 200)
        with self.assertRaises(ValueError):
            failure_reason('unknown', RuntimeError('x'))

    def test_ops_entrypoints_import_as_modules_and_as_scripts(self):
        # Provisioning once launched ops/farmctl.py as a script; its package-relative
        # imports failed and every guarded start died before touching Docker.
        root = Path(__file__).resolve().parents[1]
        for name in ('farmctl', 'provision', 'healthcheck', 'account_policy'):
            for argv in ([sys.executable, '-m', f'ops.{name}', '--help'],
                         [sys.executable, str(root / 'ops' / f'{name}.py'), '--help']):
                with self.subTest(argv=argv[1:3]):
                    result = subprocess.run(argv, cwd=root, text=True, capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr[-600:])
                    self.assertNotIn('ImportError', result.stderr)

    def test_provisioning_runs_farmctl_as_a_package_module(self):
        from ops.provision import farmctl_argv
        argv = farmctl_argv('start', 'num03', '--compose', Path('/x/compose.yml'))
        self.assertEqual(argv, [sys.executable, '-m', 'ops.farmctl', 'start', 'num03', '--compose', '/x/compose.yml'])
        source = (Path(__file__).resolve().parents[1] / 'ops' / 'provision.py').read_text()
        self.assertNotIn("'ops/farmctl.py'", source)

    def test_proxy_wait_reports_the_last_health_error(self):
        from ops import farmctl
        failing = subprocess.CompletedProcess([], 6, '', 'curl: (6) Could not resolve host: api.ipify.org\n')
        with patch('ops.farmctl.subprocess.run', return_value=failing), \
                patch('ops.farmctl.time.monotonic', side_effect=[0, 0, 500]), patch('ops.farmctl.time.sleep'), \
                self.assertRaisesRegex(RuntimeError, r'proxy did not become healthy.*Could not resolve host'):
            farmctl.wait_proxy('num01', timeout=120)

    def test_network_forensics_are_bounded_and_never_fail_the_caller(self):
        import io
        from ops import farmctl
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == 'api.ipify.org' and argv[0] == 'docker':
                raise subprocess.TimeoutExpired(argv, 10)
            return subprocess.CompletedProcess(argv, 0, 'x' * 5000, '')

        buffer = io.StringIO()
        with patch('ops.farmctl.inspect', return_value={'State': {'Running': True}}), \
                patch('ops.farmctl.subprocess.run', side_effect=fake_run):
            farmctl.network_forensics('num01', stream=buffer, limit=100)
        text = buffer.getvalue()
        self.assertIn('--- network forensics num01 ---', text)
        self.assertIn('[https-by-ip] exit=0', text)
        self.assertIn('[dns] exit=error', text)
        self.assertIn('[host-guard] exit=0', text)
        self.assertTrue(all(len(line) <= 120 for line in text.splitlines()))
        self.assertTrue(any(argv[:3] == ['docker', 'exec', 'proxy-num01'] for argv in calls))
        self.assertIn(['iptables', '-w', '-S', 'AF00001'], calls)
        buffer = io.StringIO()
        with patch('ops.farmctl.inspect', return_value=None), patch('ops.farmctl.subprocess.run', side_effect=fake_run):
            farmctl.network_forensics('num01', stream=buffer)
        self.assertIn('namespace evidence unavailable', buffer.getvalue())

    def test_diagnosis_shows_the_provisioning_log_that_names_the_device(self):
        import os
        import provisioner
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            os.environ['ANDROID_FARM_EVENT_LOG'] = str(root / 'events.jsonl')
            self.addCleanup(os.environ.pop, 'ANDROID_FARM_EVENT_LOG', None)
            state = inventory.empty_inventory()
            state['next_index'] = 4
            record, _ = inventory.choose(state, 'phone-4', 'direct:phone-4', 'request-4')
            self.assertEqual(record['id'], 'num04')
            record.update(phase='failed', egress='direct', last_error='guarded start failed')
            inventory.save(root / 'inventory.json', state)
            logs = root / 'job-logs'
            logs.mkdir(mode=0o700)
            for name, text in (('20260919T205537Z-up.log', '# up failed\nproxy did not become healthy\nnum04: preparation stopped during guarded start\n'),
                               ('20260919T210008Z-up-num04.log', '# up num04 failed\nnot startable\n'),
                               ('20260919T210111Z-up.log', '# up failed\nresume or quarantine the incomplete device\n'),
                               ('20260919T210124Z-check-num05.log', '# check num05 failed\nnum05 only\n')):
                (logs / name).write_text(text)
            config = Config(root / 'compose.yml', None, 'farm', root / 'secrets', root / 'backups',
                            'https://farm.example.com', state_dir=root)
            with patch('provisioner.docker_inspect', return_value=None), \
                    patch('provisioner.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', '')):
                report = provisioner.collect_diagnosis(config, 'num04', log_dir=logs)
            names = [item['name'] for item in report['job_logs']]
            self.assertEqual(names, ['20260919T205537Z-up.log', '20260919T210008Z-up-num04.log'])
            self.assertIn('proxy did not become healthy', report['job_logs'][0]['tail'])
            self.assertEqual(report['containers'], {'proxy': {'exists': False}, 'android': {'exists': False}, 'screen': {'exists': False}})
            self.assertEqual(report['record']['last_error'], 'guarded start failed')
            self.assertFalse(report['files']['volume'])

    def test_direct_namespace_egress_is_the_host_address_not_the_sidecar_probe(self):
        import io
        from ops import farmctl

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        with patch('urllib.request.urlopen', return_value=Response(b'8.8.8.8\n')) as opened, \
                patch('ops.farmctl.run') as sidecar:
            self.assertEqual(farmctl.namespace_egress_ip('num01', True), '8.8.8.8')
            sidecar.assert_not_called()
        self.assertEqual(opened.call_args.args[0], 'https://api.ipify.org')
        with patch('ops.farmctl.run', return_value='8.8.4.4\n') as sidecar, patch('urllib.request.urlopen') as opened:
            self.assertEqual(farmctl.namespace_egress_ip('num01', False), '8.8.4.4')
            opened.assert_not_called()
        self.assertEqual(sidecar.call_args.args[:3], ('docker', 'exec', 'proxy-num01'))
        with patch('urllib.request.urlopen', side_effect=OSError('unreachable')), \
                self.assertRaisesRegex(RuntimeError, 'host egress probe failed'):
            farmctl.host_egress_ip()

    def test_sidecar_healthcheck_accepts_a_booted_direct_namespace(self):
        script = (Path(__file__).resolve().parents[1] / 'images' / 'proxy' / 'healthcheck.sh').read_text()
        self.assertIn('if [ -f /run/direct ]; then', script)
        self.assertIn('("127.0.0.1", 5555)', script)
        self.assertTrue(script.rstrip().endswith('https://api.ipify.org >/dev/null'))


if __name__ == '__main__':
    unittest.main()
