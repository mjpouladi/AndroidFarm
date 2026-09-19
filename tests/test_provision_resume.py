"""Exercise resume through the real provisioning entrypoint with isolated I/O.

Docker, ADB, root ownership and APK verification are simulated. All files live
in a temporary directory; these tests do not claim Linux or device acceptance.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops import inventory, provision


class PrivateFixturePath(type(Path())):
    """Only simulate root-only metadata at the entrypoint's host boundary."""

    def stat(self, *args, **kwargs):
        fields = list(super().stat(*args, **kwargs))
        fields[0] &= ~0o077
        fields[4] = 0
        return os.stat_result(fields)


class ProvisionResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = PrivateFixturePath(self.temp.name)
        self.state = self.root / 'state'
        self.secret_dir = self.root / 'secrets'
        self.data_root = self.root / 'data'
        self.request_path = self.root / 'request.json'
        self.proxy_path = self.root / 'proxy.json'
        self.apk = self.root / 'internal-fixture.apk'
        self.apk.write_bytes(b'synthetic APK; verifier is mocked')
        self.request = {
            'phone': '+12025550123', 'owner_authorized': True, 'egress': 'direct',
            'apk_path': str(self.apk), 'apk_sha256': 'a' * 64,
            'apk_package': 'org.example.internalqa', 'apk_permissions': [],
        }
        self.upstream = {'type': 'http', 'server': '8.8.8.8', 'server_port': 8080,
                         'username': 'synthetic-session', 'password': 'synthetic-password'}
        self.volume = None
        self.fail_start = True
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(provision, 'os', SimpleNamespace(
            name='posix', geteuid=lambda: 0, chmod=os.chmod)))
        self.stack.enter_context(patch.object(provision, 'Path', side_effect=self.path))
        self.parse_args = self.stack.enter_context(patch.object(provision.argparse.ArgumentParser, 'parse_args',
                                            return_value=SimpleNamespace(
            request=self.request_path, resume_id=None, compose=self.root / 'compose.json', env_file=None,
            project='synthetic-resume-test', access_mode='domain', secret_dir=self.secret_dir,
            proxy_registry=self.root / 'proxies.json', proxy_store_dir=self.root / 'proxies',
            apk_trust_file=self.root / 'trust.json')))
        self.stack.enter_context(patch.dict(sys.modules, {'fcntl': SimpleNamespace(
            LOCK_EX=2, flock=lambda *_: None)}))
        self.stack.enter_context(patch.object(provision, 'open', create=True,
                                             side_effect=self.lock_file))
        self.stack.enter_context(patch.object(provision, 'read_private_json',
                                             side_effect=lambda path, _: json.loads(Path(path).read_text())))
        self.stack.enter_context(patch.object(provision, 'require_private_directory',
                                             side_effect=self.directory))
        self.stack.enter_context(patch.object(provision.resources, 'local_docker'))
        self.stack.enter_context(patch.object(provision.resources, 'probe', return_value={}))
        self.stack.enter_context(patch.object(provision.resources, 'admission'))
        self.stack.enter_context(patch.object(provision.resources, 'catalog_admission'))
        self.stack.enter_context(patch.object(provision.farmctl, 'DATA_ROOT', self.data_root))
        self.stack.enter_context(patch.object(provision.farmctl, 'data_path',
                                             side_effect=lambda device: self.data_root / device / 'data'))
        self.stack.enter_context(patch.object(provision.farmctl, 'active_devices', return_value=[]))
        self.stack.enter_context(patch.object(provision.farmctl, 'inspect', return_value=None))
        self.validate_volume = self.stack.enter_context(patch.object(
            provision.farmctl, 'validate_managed_volume'))
        self.stack.enter_context(patch.object(provision.farmctl, 'run', side_effect=self.command))
        # A direct namespace is attested through Android's shell; the proxy
        # sidecar still answers through its tunnel via ``command`` above.
        self.android_probe = self.stack.enter_context(patch.object(
            provision.farmctl, 'android_egress_ip', return_value='8.8.8.8'))
        self.stack.enter_context(patch.object(provision.subprocess, 'run', side_effect=self.volume_inspect))
        self.guarded = self.stack.enter_context(patch.object(provision, 'run_farmctl',
                                                            side_effect=self.guarded_start))
        self.stack.enter_context(patch.object(provision.app_installer, 'verify', return_value={
            'sha256': 'a' * 64, 'package': 'org.example.internalqa'}))
        self.install = self.stack.enter_context(patch.object(provision.app_installer, 'install'))
        self.stack.enter_context(patch.object(provision.events, 'note'))

    def path(self, value):
        return self.state if str(value) == '/var/lib/android-farm' else PrivateFixturePath(value)

    def lock_file(self, path, mode):
        self.assertEqual((path, mode), ('/run/lock/android-farm-provision.lock', 'w'))
        return io.StringIO()

    def directory(self, path, _description, create=False):
        path = PrivateFixturePath(path)
        self.assertTrue(path.is_relative_to(self.root))
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.assertTrue(path.is_dir())
        return path

    def volume_inspect(self, argv, **_kwargs):
        self.assertEqual(argv, ['docker', 'volume', 'inspect', 'redroid-data-num01'])
        return subprocess.CompletedProcess(argv, 0 if self.volume else 1,
                                           json.dumps([self.volume]) if self.volume else '', '')

    def command(self, *argv, **_kwargs):
        if argv[:2] == ('docker', 'compose'):
            self.assertEqual(argv[-3:], ('config', '--format', 'json'))
            return json.dumps({'services': {'android-num01': {}, 'proxy-num01': {
                'secrets': [{'source': 'egress'}]}}, 'secrets': {
                    'egress': {'file': str(self.secret_dir / 'num01.json')}}})
        if argv[:3] == ('docker', 'volume', 'create'):
            record = inventory.load(self.state / 'inventory.json')['devices']['num01']
            self.volume = {'Labels': {'farm.request': record['request_hash']}}
            return 'redroid-data-num01'
        if argv[:3] == ('docker', 'exec', 'proxy-num01'):
            return '8.8.8.8\n'
        self.fail(f'unexpected external command: {argv[:3]}')

    def guarded_start(self, action, device, *_args, **_kwargs):
        self.assertEqual(device, 'num01')
        if action == 'start':
            if self.fail_start:
                raise subprocess.CalledProcessError(1, ['synthetic-guarded-start'])
            state = inventory.load(self.state / 'inventory.json')
            state['devices'][device]['identity_baseline_created'] = True
            inventory.save(self.state / 'inventory.json', state)
        else:
            self.assertEqual(action, 'stop')

    def run_main(self):
        self.request_path.write_text(json.dumps(self.request))
        self.proxy_path.write_text(json.dumps(self.upstream))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            provision.main()

    def fail_initial_start(self, *, proxy=False):
        if proxy:
            self.request.update(egress='proxy', proxy_file=str(self.proxy_path),
                                expected_egress_ip='8.8.8.8')
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_main()
        self.fail_start = False
        self.guarded.reset_mock()
        self.install.assert_not_called()
        self.initial = inventory.load(self.state / 'inventory.json')
        self.assertEqual(self.initial['devices']['num01']['phase'], 'failed')
        self.data = self.data_root / 'num01' / 'data' / 'retained.txt'
        self.data.write_text('persistent synthetic test state')

    def assert_same_allocation(self):
        state = inventory.load(self.state / 'inventory.json')
        self.assertEqual(list(state['devices']), ['num01'])
        self.assertEqual(state['next_index'], self.initial['next_index'])
        for key in ('phone_hash', 'proxy_hash', 'request_hash'):
            self.assertEqual(state['devices']['num01'][key], self.initial['devices']['num01'][key])
        self.assertEqual(self.data.read_text(), 'persistent synthetic test state')
        return state['devices']['num01']

    def test_identical_direct_resume_reuses_allocation_volume_and_data(self):
        self.fail_initial_start()
        original_volume = self.volume
        self.run_main()
        self.assertEqual(self.assert_same_allocation()['phase'], 'ready_for_operator')
        self.assertIs(self.volume, original_volume)
        self.guarded.assert_called_once()
        self.assertEqual(self.guarded.call_args.args[:2], ('start', 'num01'))
        self.install.assert_called_once()
        self.validate_volume.assert_called_once()
        self.assertEqual(json.loads((self.secret_dir / 'num01.json').read_text()), {'type': 'direct'})

    def test_targeted_resume_reaches_guarded_start_for_the_existing_allocation(self):
        self.fail_initial_start()
        self.parse_args.return_value.resume_id = 'num01'
        self.run_main()
        self.assertEqual(self.assert_same_allocation()['phase'], 'ready_for_operator')
        self.guarded.assert_called_once()
        self.assertEqual(self.guarded.call_args.args[:2], ('start', 'num01'))

    def test_targeted_resume_cannot_bootstrap_a_missing_allocation(self):
        self.parse_args.return_value.resume_id = 'num01'
        with self.assertRaisesRegex(RuntimeError, 'resume target is not allocated'):
            self.run_main()
        self.assertFalse(self.state.exists())
        self.assertFalse(self.secret_dir.exists())
        self.assertFalse(self.data_root.exists())
        self.assertIsNone(self.volume)
        self.guarded.assert_not_called()
        self.install.assert_not_called()

    def test_targeted_resume_mismatch_preserves_state_before_any_start(self):
        self.fail_initial_start()
        before = (self.state / 'inventory.json').read_bytes()
        secret_before = (self.secret_dir / 'num01.json').read_bytes()
        key_before = (self.state / 'identity.key').read_bytes()
        for target, phone, error in (
                ('num01', '+12025550124', 'resume phone does not match the selected device'),
                ('num02', '+12025550123', 'resume target is not allocated')):
            with self.subTest(target=target):
                self.parse_args.return_value.resume_id = target
                self.request['phone'] = phone
                with self.assertRaisesRegex(RuntimeError, error):
                    self.run_main()
                self.assertEqual((self.state / 'inventory.json').read_bytes(), before)
                self.assertEqual((self.secret_dir / 'num01.json').read_bytes(), secret_before)
                self.assertEqual((self.state / 'identity.key').read_bytes(), key_before)
                self.assert_same_allocation()
        self.guarded.assert_not_called()
        self.install.assert_not_called()

    def test_direct_resume_rejects_altered_or_mixed_installed_secret(self):
        self.fail_initial_start()
        before = (self.state / 'inventory.json').read_bytes()
        secret_path = self.secret_dir / 'num01.json'
        for installed in ({'type': 'http'}, {'type': 'direct', 'server': '8.8.8.8'},
                          {'type': 'direct', 'username': None}, {'type': 'direct', 'password': ''}, {}):
            with self.subTest(installed=installed):
                secret_path.write_text(json.dumps(installed))
                with self.assertRaisesRegex(RuntimeError, 'direct egress configuration differs'):
                    self.run_main()
                self.assertEqual((self.state / 'inventory.json').read_bytes(), before)
                self.assertEqual(json.loads(secret_path.read_text()), installed)
                self.assert_same_allocation()
        self.guarded.assert_not_called()
        self.install.assert_not_called()

    def test_other_phone_or_changed_request_cannot_resume_allocation(self):
        self.fail_initial_start()
        before = (self.state / 'inventory.json').read_bytes()
        self.request['phone'] = '+12025550124'
        with self.assertRaisesRegex(RuntimeError, 'incomplete device before adding another'):
            self.run_main()
        self.request['phone'] = '+12025550123'
        self.request['apk_permissions'] = ['android.permission.CAMERA']
        with self.assertRaisesRegex(RuntimeError, 'existing phone has a different request'):
            self.run_main()
        self.assertEqual((self.state / 'inventory.json').read_bytes(), before)
        self.assert_same_allocation()
        self.guarded.assert_not_called()
        self.install.assert_not_called()

    def test_proxy_password_rotation_keeps_existing_resume_behavior(self):
        self.fail_initial_start(proxy=True)
        self.upstream['password'] = 'synthetic-rotated-password'
        self.run_main()
        self.assertEqual(self.assert_same_allocation()['phase'], 'ready_for_operator')
        self.assertEqual(json.loads((self.secret_dir / 'num01.json').read_text())['password'],
                         'synthetic-rotated-password')

    def test_proxy_resume_rejects_changed_installed_endpoint_or_direct_mode(self):
        self.fail_initial_start(proxy=True)
        before = (self.state / 'inventory.json').read_bytes()
        for installed in (dict(self.upstream, server='8.8.4.4'), {'type': 'direct'}):
            with self.subTest(installed=installed):
                (self.secret_dir / 'num01.json').write_text(json.dumps(installed))
                with self.assertRaisesRegex(RuntimeError, 'proxy endpoint/session differs'):
                    self.run_main()
                self.assertEqual((self.state / 'inventory.json').read_bytes(), before)
                self.assert_same_allocation()
        self.guarded.assert_not_called()
        self.install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
