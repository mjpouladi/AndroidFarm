from contextlib import nullcontext
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ops import inventory
from ops.secureio import atomic_json
from provisioner import Config
from services.api.operations import Operations, OperationError, bounded_process


class ApiOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(self.root / 'compose.yml', None, 'farm', self.root / 'secrets',
                             self.root / 'backups', 'https://farm.example.com',
                             state_dir=self.root, apk_trust_file=self.root / 'trust.json',
                             proxy_registry=self.root / 'proxies.json', proxy_store_dir=self.root / 'proxies')
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.ops = Operations(self.root / 'config.json', catalog_path=self.root / 'apps.json', runner=self.runner)
        self.config_patch = patch.object(self.ops, '_config', return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        # Fixtures are under the platform's temporary directory. Production's
        # root ancestor rule remains exercised separately via the helper call.
        trust_patch = patch('services.api.operations.require_trusted_release_file', side_effect=lambda path, *_: Path(path))
        trust_patch.start()
        self.addCleanup(trust_patch.stop)
        state = inventory.empty_inventory()
        record, _ = inventory.choose(state, 'phone-hash', 'proxy-hash', 'request-hash')
        record.update(proxy_id='qa-proxy', phone_masked='+12***1234', phase='ready_for_operator',
                      expected_egress_ip='8.8.8.8')
        inventory.save(self.root / 'inventory.json', state)

    def app_catalog(self):
        apk = self.root / 'reviewed.apk'
        apk.write_bytes(b'reviewed APK')
        apk.chmod(0o600)
        app = dict(id='qa-app', label='QA application', apk_path=str(apk), apk_sha256='a' * 64,
                   apk_package='com.example.qa', apk_activity='.Main',
                   apk_permissions=['android.permission.CAMERA'])
        atomic_json(self.root / 'apps.json', {'schema_version': 1, 'apps': [app]})
        return app

    def test_rejects_extra_fields_injection_unknown_device_and_aliases(self):
        invalid = [
            {'action': 'up', 'device': 'num01', 'argv': ['--apk', '/tmp/payload']},
            {'action': 'up', 'device': 'num01', 'params': {'apk_path': '/etc/shadow'}},
            {'action': 'up', 'device': 'num01; touch /tmp/unsafe'},
            {'action': 'up', 'device': 'dev01'},
            {'action': 'down', 'device': 'num02'},
            {'action': 'up', 'device': True},
            {'action': 'shell', 'params': {}},
            {'action': 'proxy-test', 'device': 'num01', 'params': {'id': 'qa-proxy'}},
        ]
        for job in invalid:
            with self.subTest(job=job), self.assertRaises(ValueError):
                self.ops.validate_job(job)
        self.runner.assert_not_called()

    def test_lifecycle_uses_fixed_argv_and_does_not_return_subprocess_text(self):
        self.runner.return_value.stdout = 'private-password=never-show-this'
        result = self.ops.execute({'action': 'up', 'device': 'num01'})
        args, kwargs = self.runner.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(args[0][-3:], ['up', '--id', 'num01'])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(result['screen_path'], '/d/num01/')
        self.assertNotIn('never-show-this', json.dumps(result))
        self.runner.return_value.returncode = 1
        with self.assertRaises(OperationError) as failure:
            self.ops.execute({'action': 'down', 'device': 'num01'})
        self.assertNotIn('never-show-this', str(failure.exception))
        self.runner.return_value.stdout = 'never-show-this\ncalculated concurrent capacity reached'
        with self.assertRaisesRegex(OperationError, 'stop a device') as failure:
            self.ops.execute({'action': 'up', 'device': 'num01'})
        self.assertNotIn('never-show-this', str(failure.exception))

    def test_ip_check_returns_only_validated_evidence(self):
        self.runner.return_value.stdout = json.dumps({'proxy_namespace': '8.8.8.8',
                                                      'android_shell': '8.8.8.8', 'expected': '8.8.8.8',
                                                      'matches': True, 'password': 'secret'})
        result = self.ops.execute({'action': 'check-ip', 'device': 'num01'})
        self.assertTrue(result['matches'])
        self.assertNotIn('password', result)
        self.assertEqual(self.runner.call_args.args[0][-1], '--json')
        self.runner.return_value.stdout = '{"proxy_namespace": "not-an-IP"}'
        with self.assertRaises(OperationError):
            self.ops.execute({'action': 'check-ip', 'device': 'num01'})

    def test_release_requires_explicit_completed_review_and_never_starts_device(self):
        for params in ({}, {'review_completed': False}, {'review_completed': 1},
                       {'review_completed': 'true'}, {'review_completed': True, 'start': True}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.ops.validate_job({'action': 'release', 'device': 'num01', 'params': params})
        self.runner.assert_not_called()
        result = self.ops.execute({'action': 'release', 'device': 'num01',
                                   'params': {'review_completed': True}})
        self.assertTrue(result['completed'])
        self.assertEqual(self.runner.call_args.args[0][-4:],
                         ['release', '--id', 'num01', '--review-completed'])
        self.runner.assert_called_once()
        self.assertNotIn('screen_path', result)

    def test_provision_uses_approved_catalog_and_removes_private_request(self):
        app = self.app_catalog()
        store = Mock()
        store.show.return_value = {'state': 'enabled'}
        seen = []

        def run(argv, **_):
            self.assertEqual(argv[-2], '--request')
            path = Path(argv[-1])
            seen.append(path)
            request = json.loads(path.read_text())
            self.assertEqual(request['apk_path'], app['apk_path'])
            self.assertEqual(request['apk_sha256'], 'a' * 64)
            self.assertEqual(request['phone'], '+12025551234')
            self.assertNotIn('artifact_id', request)
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return subprocess.CompletedProcess(argv, 0, 'num01: ready\nscreen: /d/num01/\n', '')

        self.runner.side_effect = run
        job = {'action': 'provision', 'params': {'phone': '+12025551234', 'owner_authorized': True,
                                                'proxy_id': 'qa-proxy', 'artifact_id': 'qa-app'}}
        with patch.object(self.ops, '_store', return_value=store):
            result = self.ops.execute(job)
        self.assertEqual(result['device'], 'num01')
        self.assertFalse(seen[0].exists())
        self.assertNotIn('+12025551234', json.dumps(result))
        job['params']['apk_path'] = '/tmp/unreviewed.apk'
        with self.assertRaises(ValueError):
            self.ops.validate_job(job)

    def test_failed_provision_also_removes_private_request(self):
        self.app_catalog()
        store = Mock()
        store.show.return_value = {'state': 'enabled'}
        self.runner.return_value.returncode = 1
        with patch.object(self.ops, '_store', return_value=store), self.assertRaises(OperationError):
            self.ops.execute({'action': 'provision', 'params': {'phone': '+12025551234',
                             'owner_authorized': True, 'proxy_id': 'qa-proxy', 'artifact_id': 'qa-app'}})
        self.assertEqual(list((self.root / 'web-requests').iterdir()), [])

    def test_invalid_catalog_and_untrusted_artifact_are_not_available(self):
        app = self.app_catalog()
        for change in ({'apk_path': '../unsafe.apk'}, {'apk_permissions': ['android.permission.ROOT']},
                       {'apk_activity': '; shell'}, {'apk_sha256': 'x' * 64}, {'extra': 'field'}):
            atomic_json(self.root / 'apps.json', {'schema_version': 1, 'apps': [dict(app, **change)]})
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.ops._catalog()
        with patch('services.api.operations.require_trusted_release_file', side_effect=RuntimeError('unsafe parent')):
            self.assertFalse(self.ops._artifact_available(app))

    def test_proxy_validation_rejects_private_endpoints_and_secret_options(self):
        params = dict(id='qa-proxy', label='QA', type='http', server='8.8.8.8', server_port=8080,
                      username='test-account', password='never-show-this', expected_egress_ip='1.1.1.1')
        self.assertEqual(self.ops.validate_job({'action': 'proxy-add', 'params': params})['params'], params)
        for change in ({'server': '127.0.0.1'}, {'server_port': True}, {'type': 'file'},
                       {'password_file': '/etc/shadow'}, {'id': '../escape'}, {'password': 'a\nb'}, {'type': []}):
            with self.subTest(change=change), self.assertRaises(ValueError) as failure:
                self.ops.validate_job({'action': 'proxy-add', 'params': dict(params, **change)})
            self.assertNotIn('never-show-this', str(failure.exception))

    def test_proxy_enable_rolls_back_and_assigned_disable_stops_under_lock(self):
        store = Mock()
        store.show.return_value = {'assigned_device': 'num01'}
        store.check_health.side_effect = RuntimeError('raw-error-with-private-password')
        with patch.object(self.ops, '_store', return_value=store), \
                patch('services.api.operations.provisioner.host_lifecycle_lock', return_value=nullcontext()):
            with self.assertRaises(OperationError) as failure:
                self.ops.execute({'action': 'proxy-enable', 'params': {'id': 'qa-proxy'}})
        self.assertNotIn('private-password', str(failure.exception))
        store.disable.assert_called_once_with('qa-proxy')
        store.disable.return_value = {'id': 'qa-proxy', 'state': 'disabled'}
        with patch.object(self.ops, '_store', return_value=store), \
                patch('services.api.operations.provisioner.host_lifecycle_lock', return_value=nullcontext()):
            result = self.ops.execute({'action': 'proxy-disable', 'params': {'id': 'qa-proxy'}})
        self.assertTrue(result['proxy']['safety_hold_created'])
        self.assertEqual(self.runner.call_args.args[0][-5:], ['hold', '--id', 'num01', '--reason', 'maintenance'])

    def test_snapshot_stopped_health_is_not_live_and_backup_metadata_is_real(self):
        self.app_catalog()
        folder = self.root / 'backups' / 'num01'
        folder.mkdir(parents=True, mode=0o700)
        folder.parent.chmod(0o700)
        archive = folder / '20260919T120000Z.tar'
        archive.write_bytes(b'actual backup bytes')
        archive.chmod(0o600)
        (folder / 'partial.tar.partial').write_bytes(b'incomplete')
        store = Mock()
        store.list.return_value = [{'id': 'qa-proxy', 'type': 'http', 'server': '8.8.8.8',
                                    'server_port': 8080, 'credential': 't***t'}]
        inspections = {f'{role}-num01': {'State': {'Running': False, 'Health': {'Status': 'healthy'}}}
                       for role in ('proxy', 'android', 'screen')}
        with patch.object(self.ops, '_store', return_value=store), \
                patch('services.api.operations.resources.probe', return_value={'capacity': 10}), \
                patch('services.api.operations.provisioner.bulk_managed_inspections', return_value=inspections):
            result = self.ops.snapshot()
        row = result['devices'][0]
        self.assertFalse(row['screen_ready'])
        self.assertEqual(set(row['containers'].values()), {'stopped'})
        self.assertIsNone(row['cpu'])
        self.assertIsNone(row['memory'])
        self.assertEqual(result['backups'][0]['size_bytes'], archive.stat().st_size)
        self.assertEqual(result['backups'][0]['created_at'], int(archive.stat().st_mtime))
        self.assertEqual(len(result['backups']), 1)
        self.assertNotIn('apk_path', result['artifacts'][0])
        self.assertNotIn('session_sha256', json.dumps(result))

    def test_snapshot_missing_container_and_partial_failure_do_not_invent_data(self):
        store = Mock()
        store.list.side_effect = RuntimeError('registry-password=secret')
        with patch.object(self.ops, '_store', return_value=store), \
                patch('services.api.operations.resources.probe', side_effect=RuntimeError('private path')), \
                patch('services.api.operations.provisioner.bulk_managed_inspections', return_value={}):
            result = self.ops.snapshot()
        self.assertIsNone(result['resources'])
        self.assertEqual(result['proxies'], [])
        self.assertEqual(result['artifacts'], [])
        self.assertEqual(set(result['devices'][0]['containers'].values()), {'missing'})
        self.assertFalse(result['devices'][0]['screen_ready'])
        self.assertGreaterEqual(len(result['errors']), 3)
        self.assertNotIn('registry-password', json.dumps(result))

    def test_snapshot_running_measurements_are_actual_docker_values(self):
        store = Mock()
        store.list.return_value = []
        inspections = {f'{role}-num01': {'State': {'Running': True, 'Health': {'Status': 'healthy'}}}
                       for role in ('proxy', 'android', 'screen')}
        stats = json.dumps({'Name': 'android-num01', 'CPUPerc': '24.3%', 'MemUsage': '1GiB / 4GiB'})
        with patch.object(self.ops, '_store', return_value=store), \
                patch('services.api.operations.resources.probe', return_value={'capacity': 10}), \
                patch('services.api.operations.provisioner.bulk_managed_inspections', return_value=inspections), \
                patch('services.api.operations.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stats, '')):
            result = self.ops.snapshot()
        self.assertTrue(result['devices'][0]['screen_ready'])
        self.assertEqual(result['devices'][0]['cpu'], '24.3%')
        self.assertEqual(result['devices'][0]['memory'], '1GiB / 4GiB')
        atomic_json(self.root / 'holds.json', {'num01': False})
        with patch.object(self.ops, '_store', return_value=store), \
                patch('services.api.operations.resources.probe', return_value={'capacity': 10}), \
                patch('services.api.operations.provisioner.bulk_managed_inspections', return_value=inspections), \
                patch('services.api.operations.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stats, '')):
            held = self.ops.snapshot()
        self.assertFalse(held['devices'][0]['screen_ready'])
        self.assertEqual(held['devices'][0]['hold']['reason'], 'unknown')

    def test_subprocess_output_is_bounded_while_pipe_is_fully_drained(self):
        result = bounded_process([sys.executable, '-c', 'import sys; sys.stdout.write("x" * 300000)'],
                                 cwd=str(self.root), timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout), 65536)
        self.assertGreater(len(result.stdout), 60000)

    def test_subprocess_timeout_is_safe(self):
        with self.assertRaisesRegex(OperationError, 'timed out'):
            bounded_process([sys.executable, '-c', 'import time; time.sleep(30)'],
                            cwd=str(self.root), timeout=.05)


@unittest.skipUnless(sys.platform == 'linux', 'Linux parent-death integration requires prctl and /proc')
class LinuxProcessSupervisionTests(unittest.TestCase):
    @staticmethod
    def alive(pid):
        try:
            # A terminated orphan may await init reaping; a zombie no longer
            # executes CLI work and must not be confused with a surviving job.
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            return state != 'Z'
        except FileNotFoundError:
            return False

    def test_parent_death_stops_supervisor_child_and_term_ignoring_grandchild(self):
        from services.api.operations import ROOT
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'processes.json'
            grandchild = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'
            child = (
                'import json,os,pathlib,signal,subprocess,sys,time; '
                'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                f'p=subprocess.Popen([sys.executable,"-c",{grandchild!r}]); '
                'pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getppid(),os.getpid(),p.pid])); '
                'time.sleep(60)'
            )
            worker = (
                'import os,subprocess,sys,time; '
                'subprocess.Popen([sys.executable,sys.argv[1],"--parent-pid",str(os.getpid()),'
                f'"--",sys.executable,"-c",{child!r},sys.argv[2]],start_new_session=True); '
                'time.sleep(60)'
            )
            process = subprocess.Popen([sys.executable, '-c', worker,
                                        str(ROOT / 'services/api/runner.py'), str(marker)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pids = []
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not marker.exists():
                    time.sleep(.02)
                self.assertTrue(marker.exists(), 'supervised child did not launch')
                pids = json.loads(marker.read_text())
                process.kill()  # Simulate abrupt Gunicorn worker loss, not graceful shutdown.
                process.wait(timeout=5)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and any(self.alive(pid) for pid in pids):
                    time.sleep(.05)
                self.assertFalse(any(self.alive(pid) for pid in pids), 'CLI descendants survived worker death')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if pids and self.alive(pids[0]):
                    os.killpg(pids[0], signal.SIGKILL)

    def test_parent_pid_race_refuses_to_launch_the_command(self):
        from services.api.operations import ROOT
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'must-not-exist'
            result = subprocess.run(
                [sys.executable, str(ROOT / 'services/api/runner.py'), '--parent-pid',
                 str(os.getpid() + 1000000), '--', sys.executable, '-c',
                 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("unsafe")', str(marker)],
                start_new_session=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 125)
            self.assertFalse(marker.exists())

    def test_supervisor_refuses_an_inherited_process_group(self):
        from services.api.operations import ROOT
        result = subprocess.run([sys.executable, str(ROOT / 'services/api/runner.py'),
                                 '--parent-pid', str(os.getpid()), '--', sys.executable, '-c', 'print("unsafe")'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 125)
        self.assertNotIn('unsafe', result.stdout)


if __name__ == '__main__':
    unittest.main()
