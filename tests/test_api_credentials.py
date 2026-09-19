import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from services.api.credentials import (CredentialManager, CredentialsError, DEFAULT_MIDDLEWARE,
                                      GATEWAY_INIT_SCRIPT, GRAFANA_INIT_SCRIPT, GRAFANA_LEGACY_INIT_SCRIPT,
                                      middleware_matches, render_middleware, validate_credentials)


class CredentialPolicyTests(unittest.TestCase):
    def test_password_policy_is_bounded_utf8_and_never_echoes_input(self):
        validate_credentials('qa-admin', 'long-test-password')
        validate_credentials('operator', 'ع' * 6)
        for username, password in [('--flag', 'long-test-password'), ('a:b', 'long-test-password'),
                                   ('user', 'tiny'), ('user', 'x' * 73), ('user', 'test-password\n'),
                                   ('user', ' test-password'), ('user', 'test-password '),
                                   ('user', 'null-password\0'), ('user', '\ud800' * 15)]:
            with self.subTest(username=username), self.assertRaises(CredentialsError) as failure:
                validate_credentials(username, password)
            self.assertNotIn(password, str(failure.exception))

    def test_middleware_revision_changes_configuration_without_changing_auth_policy(self):
        first = render_middleware(DEFAULT_MIDDLEWARE, 'a' * 64)
        second = render_middleware(first, 'b' * 64)
        self.assertTrue(middleware_matches(first, DEFAULT_MIDDLEWARE))
        self.assertTrue(middleware_matches(second, DEFAULT_MIDDLEWARE))
        self.assertNotEqual(first, second)
        self.assertEqual(second.count(b'realm:'), 2)
        self.assertFalse(middleware_matches(second.replace(b'removeHeader: true', b'removeHeader: false'),
                                            DEFAULT_MIDDLEWARE))
        self.assertFalse(middleware_matches(second + b'  unknown: true\n', DEFAULT_MIDDLEWARE))
        with self.assertRaises(CredentialsError):
            render_middleware(DEFAULT_MIDDLEWARE, 'password-is-not-a-revision')


@unittest.skipUnless(os.name != 'posix' or os.geteuid() == 0, 'managed credential files require root')
class CredentialManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='farm-credential-test-',
                                                    dir='/run' if os.name == 'posix' else None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config, self.state, self.dynamic = [self.root / name for name in ('config', 'state', 'dynamic')]
        for directory in (self.config, self.state, self.dynamic, self.config / 'monitoring'):
            directory.mkdir(mode=0o700)
        self.auth_file = self.dynamic / 'farm-users.htpasswd'
        self.write(self.auth_file, 'operator:$2y$12$old-test-hash\n')
        self.write(self.dynamic / 'farm-auth.yml', DEFAULT_MIDDLEWARE.decode(), mode=0o644)
        self.write(self.config / 'web-login-password', 'existing-web-password\n')
        self.write(self.config / 'web-login-user', 'operator\n')
        self.write(self.config / 'monitoring/grafana-admin-password', 'existing-monitor-password\n')
        self.write(self.config / 'monitoring/grafana-admin-user', 'admin\n')
        self.write(self.config / 'compose.env', 'GRAFANA_ADMIN_USER=admin\nFARM_HTTP_AUTH_REVISION=' + 'a' * 64 + '\n')
        self.write(self.config / 'coolify.env', 'GRAFANA_ADMIN_USER=admin\nFARM_HTTP_AUTH_REVISION=' + 'a' * 64 + '\n')
        self.write(self.state / 'quickstart.json', json.dumps({'schema_version': 1, 'auth_user': 'operator',
                                                             'installation_id': 'test-installation'}))
        self.commands, self.http_calls = [], []
        self.current_user, self.current_password = 'admin', 'existing-monitor-password'
        self.fail_init_once = None
        self.fail_after_password = False
        self.fail_all_http = False
        self.containers = {}
        for name, role in [('android-farm-gateway', 'gateway'), ('android-farm-grafana', 'grafana')]:
            volume = 'test-gateway' if role == 'gateway' else 'test-grafana'
            target = '/run/gateway-private' if role == 'gateway' else '/run/grafana-private'
            self.containers[name] = {'Name': '/' + name, 'Config': {'Labels': {'farm.stack': 'core', 'farm.role': role,
                                      'com.docker.compose.project': 'android-farm-core',
                                      'com.docker.compose.service': role},
                                      'Env': ['GF_SECURITY_ADMIN_USER=admin']}, 'State': {'Running': True},
                                     'Mounts': [{'Type': 'volume', 'Name': volume, 'Destination': target, 'RW': False}],
                                     'NetworkSettings': {'Networks': {'managed': {'IPAddress': '172.30.0.3'}}}}
        self.containers['android-farm-grafana']['Mounts'].append(
            {'Type': 'volume', 'Name': 'test-grafana-data', 'Destination': '/var/lib/grafana', 'RW': True})
        for name, source, target, volume in [
            ('android-farm-gateway-secret-init', self.auth_file, '/run/secrets/farm-http-auth', 'test-gateway'),
            ('android-farm-grafana-secret-init', self.config / 'monitoring/grafana-admin-password',
             '/run/secrets/grafana-admin-password', 'test-grafana')]:
            grafana = 'grafana' in name
            self.containers[name] = {'Name': '/' + name, 'Config': {'Labels': {'farm.stack': 'core', 'farm.role': 'secret-init',
                                                                'com.docker.compose.project': 'android-farm-core',
                                                                'com.docker.compose.service': 'grafana-secret-init' if grafana else 'gateway-secret-init'},
                                                                'User': '0:0', 'Image': 'alpine:3.21', 'Entrypoint': None,
                                                                'Cmd': ['/bin/sh', '-ec', GRAFANA_INIT_SCRIPT if grafana else GATEWAY_INIT_SCRIPT]},
                                     'State': {'Running': False, 'ExitCode': 0},
                                     'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
                                                    'CapDrop': ['ALL'], 'CapAdd': ['CHOWN', 'DAC_OVERRIDE', 'FOWNER'],
                                                    'SecurityOpt': ['no-new-privileges:true']},
                                     'Mounts': [{'Type': 'bind', 'Source': str(source), 'Destination': target, 'RW': False},
                                                {'Type': 'volume', 'Name': volume, 'Destination': '/private', 'RW': True}]}
            if grafana:
                self.containers[name]['Mounts'].extend([
                    {'Type': 'volume', 'Name': 'test-grafana-data', 'Destination': '/grafana-data', 'RW': True},
                    {'Type': 'bind', 'Source': str(self.config / 'monitoring/grafana-admin-user'),
                     'Destination': '/run/secrets/grafana-admin-user', 'RW': False}])
        self.manager = CredentialManager(self.auth_file, self.config, self.state,
                                         runner=self.runner, requester=self.requester)

    @staticmethod
    def write(path, content, mode=0o600):
        path.write_text(content, encoding='utf-8')
        path.chmod(mode)

    def runner(self, argv, **kwargs):
        self.commands.append((list(argv), kwargs))
        if argv[:2] == ['htpasswd', '-niB']:
            return subprocess.CompletedProcess(argv, 0, argv[2] + ':$2y$12$new-test-hash\n', '')
        if argv[:2] == ['docker', 'inspect']:
            return subprocess.CompletedProcess(argv, 0, json.dumps([copy.deepcopy(self.containers[argv[2]])]), '')
        if argv[:3] == ['docker', 'start', '--attach']:
            if argv[-1] == self.fail_init_once:
                self.fail_init_once = None
                return subprocess.CompletedProcess(argv, 1, 'private-canary-error', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        raise AssertionError('unexpected command')

    def requester(self, method, url, user, password, payload=None):
        self.http_calls.append((method, url, user, password, payload))
        if self.fail_all_http or user != self.current_user or password != self.current_password:
            raise CredentialsError('private-canary-error')
        if method == 'GET' and url.endswith('/api/user'):
            return {'id': 1, 'login': self.current_user, 'isGrafanaAdmin': True,
                    'name': 'Existing Admin', 'email': 'admin@example.test', 'theme': 'dark'}
        if method == 'PUT' and url.endswith('/api/user/password'):
            self.assertEqual(payload['oldPassword'], self.current_password)
            self.current_password = payload['newPassword']
            if self.fail_after_password:
                self.fail_after_password = False
                raise CredentialsError('request timed out after server commit')
            return {'message': 'User password changed'}
        if method == 'PUT' and url.endswith('/api/users/1'):
            self.assertEqual(payload['email'], 'admin@example.test')
            self.current_user = payload['login']
            return {'message': 'User updated'}
        raise AssertionError('unexpected HTTP request')

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*')
                if path.is_file() and path.name != 'credential.lock'}

    def test_platform_rotation_updates_database_private_files_env_and_only_init_containers(self):
        result = self.manager.rotate('platform', 'new-operator', 'new-platform-password')
        self.assertEqual(self.current_user, 'new-operator')
        self.assertEqual(self.current_password, 'new-platform-password')
        self.assertEqual(result['changed'], ['web', 'grafana'])
        self.assertTrue(result['relogin_required'])
        self.assertNotIn('new-platform-password', json.dumps(result))
        for name in ('web-login-password', 'monitoring/grafana-admin-password'):
            self.assertEqual((self.config / name).read_text().strip(), 'new-platform-password')
        for name in ('web-login-user', 'monitoring/grafana-admin-user'):
            self.assertEqual((self.config / name).read_text().strip(), 'new-operator')
        for name in ('compose.env', 'coolify.env'):
            self.assertIn('GRAFANA_ADMIN_USER=new-operator', (self.config / name).read_text())
        self.assertEqual(json.loads((self.state / 'quickstart.json').read_text())['auth_user'], 'new-operator')
        self.assertTrue(self.auth_file.read_text().startswith('new-operator:$2'))
        self.assertTrue(middleware_matches((self.dynamic / 'farm-auth.yml').read_bytes(), DEFAULT_MIDDLEWARE))
        started = [argv[-1] for argv, _ in self.commands if argv[:2] == ['docker', 'start']]
        self.assertEqual(started, ['android-farm-grafana-secret-init', 'android-farm-gateway-secret-init'])
        for argv, _ in self.commands:
            self.assertNotIn('new-platform-password', ' '.join(argv))
        if os.name == 'posix':
            self.assertEqual(self.auth_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.config / 'web-login-password').stat().st_mode & 0o777, 0o600)

    def test_web_only_does_not_touch_grafana_and_returns_safe_summary(self):
        self.manager.rotate('web', 'new-operator', 'new-platform-password')
        self.assertEqual(self.http_calls, [])
        summary = self.manager.summary()
        self.assertEqual(summary['web_username'], 'new-operator')
        self.assertEqual(summary['grafana_username'], 'admin')
        self.assertNotIn('password', json.dumps(summary))

    def test_grafana_only_keeps_existing_browser_login(self):
        before = self.auth_file.read_bytes()
        result = self.manager.rotate('grafana', 'admin', 'new-monitor-password')
        self.assertFalse(result['relogin_required'])
        self.assertEqual(self.auth_file.read_bytes(), before)
        self.assertEqual(self.current_password, 'new-monitor-password')
        self.assertFalse(any(url.endswith('/api/users/1') for _, url, *_ in self.http_calls))

    def test_platform_failure_restores_both_accounts_and_all_original_files(self):
        before = self.files()
        self.fail_init_once = 'android-farm-gateway-secret-init'
        with self.assertRaises(CredentialsError) as failure:
            self.manager.rotate('platform', 'new-operator', 'new-platform-password')
        self.assertNotIn('private-canary-error', str(failure.exception))
        self.assertIn('[web-secret-sync/tool-failed]', str(failure.exception))
        self.assertEqual(self.files(), before)
        self.assertEqual((self.current_user, self.current_password), ('admin', 'existing-monitor-password'))

    def test_timeout_after_grafana_password_commit_is_discovered_and_rolled_back(self):
        before = self.files()
        self.fail_after_password = True
        with self.assertRaises(CredentialsError):
            self.manager.rotate('grafana', 'new-operator', 'new-platform-password')
        self.assertEqual(self.current_password, 'existing-monitor-password')
        self.assertEqual(self.current_user, 'admin')
        self.assertEqual(self.files(), before)

    def test_preflight_failure_reports_stage_without_secret_exception_text(self):
        before = self.files()
        self.fail_all_http = True
        with self.assertRaises(CredentialsError) as failure:
            self.manager.rotate('platform', 'new-operator', 'new-platform-password')
        self.assertIn('[grafana-preflight/credential-operation]', str(failure.exception))
        self.assertNotIn('private-canary-error', str(failure.exception))
        self.assertEqual(self.files(), before)
        self.assertFalse(any(method != 'GET' for method, *_ in self.http_calls))
        self.assertFalse(any(argv[:2] == ['docker', 'start'] for argv, _ in self.commands))

    def test_preflight_rejects_wrong_mount_identity_without_mutating_credentials(self):
        before = self.files()
        self.containers['android-farm-gateway-secret-init']['Mounts'][0]['Source'] = '/etc/shadow'
        with self.assertRaises(CredentialsError):
            self.manager.rotate('platform', 'new-operator', 'new-platform-password')
        self.assertEqual(self.files(), before)
        self.assertEqual(self.http_calls, [])
        self.assertFalse(any(argv[:2] == ['docker', 'start'] for argv, _ in self.commands))

    def test_init_scripts_match_current_compose_and_legacy_grafana_is_supported(self):
        import yaml
        compose = yaml.safe_load((Path(__file__).resolve().parents[1] / 'docker-compose.yml').read_text())
        self.assertEqual(compose['services']['gateway-secret-init']['command'], ['/bin/sh', '-ec', GATEWAY_INIT_SCRIPT])
        self.assertEqual(compose['services']['grafana-secret-init']['command'], ['/bin/sh', '-ec', GRAFANA_INIT_SCRIPT])
        init = self.containers['android-farm-grafana-secret-init']
        init['Config']['Cmd'][2] = GRAFANA_LEGACY_INIT_SCRIPT
        init['Mounts'] = [mount for mount in init['Mounts'] if mount['Destination'] != '/run/secrets/grafana-admin-user']
        self.manager.rotate('grafana', 'new-operator', 'new-monitor-password')
        self.assertEqual(self.current_user, 'new-operator')

    def test_modified_init_command_privileges_or_mounts_are_rejected_before_mutation(self):
        name = 'android-farm-gateway-secret-init'
        baseline = copy.deepcopy(self.containers[name])
        mutations = [
            lambda item: item['Config'].update(Cmd=['/bin/sh', '-ec', GATEWAY_INIT_SCRIPT + '; touch /private/injected']),
            lambda item: item['Config'].update(Entrypoint=['/bin/sh', '-c']),
            lambda item: item['Config'].update(Image='unreviewed:latest'),
            lambda item: item['Config']['Labels'].update({'com.docker.compose.project': 'foreign-project'}),
            lambda item: item['Config']['Labels'].update({'com.docker.compose.service': 'foreign-service'}),
            lambda item: item['HostConfig'].update(Privileged=True),
            lambda item: item['HostConfig'].update(CapDrop=[]),
            lambda item: item['HostConfig'].update(CapAdd=['SYS_ADMIN']),
            lambda item: item['HostConfig'].update(SecurityOpt=[]),
            lambda item: item['HostConfig'].update(PidMode='host'),
            lambda item: item['HostConfig'].update(IpcMode='host'),
            lambda item: item['HostConfig'].update(Devices=[{'PathOnHost': '/dev/sda'}]),
            lambda item: item['Mounts'].append({'Type': 'bind', 'Source': '/etc', 'Destination': '/extra', 'RW': True}),
        ]
        before = self.files()
        for mutate in mutations:
            self.containers[name] = copy.deepcopy(baseline)
            mutate(self.containers[name])
            with self.subTest(mutation=mutate), self.assertRaises(CredentialsError):
                self.manager.rotate('platform', 'new-operator', 'new-platform-password')
            self.assertEqual(self.files(), before)
            self.assertEqual(self.http_calls, [])
            self.assertFalse(any(argv[:2] == ['docker', 'start'] for argv, _ in self.commands))

    def test_grafana_data_and_username_mounts_cannot_be_redirected(self):
        init = self.containers['android-farm-grafana-secret-init']
        baseline = copy.deepcopy(init['Mounts'])
        before = self.files()
        for destination, field, value in [('/grafana-data', 'Name', 'unrelated-data'),
                                          ('/run/secrets/grafana-admin-user', 'Source', '/etc/shadow'),
                                          ('/run/secrets/grafana-admin-user', 'RW', True)]:
            init['Mounts'] = copy.deepcopy(baseline)
            next(m for m in init['Mounts'] if m['Destination'] == destination)[field] = value
            with self.subTest(destination=destination, field=field), self.assertRaises(CredentialsError):
                self.manager.rotate('grafana', 'new-operator', 'new-monitor-password')
            self.assertEqual(self.files(), before)
            self.assertFalse(any(method == 'PUT' for method, *_ in self.http_calls))
            self.assertFalse(any(argv[:2] == ['docker', 'start'] for argv, _ in self.commands))

    def test_metadata_ip_or_host_network_cannot_be_used_for_grafana_http(self):
        for address in ('169.254.169.254', '127.0.0.1', '8.8.8.8', '192.0.0.2', '0.0.0.1', 'http://elsewhere'):
            self.containers['android-farm-grafana']['NetworkSettings']['Networks']['managed']['IPAddress'] = address
            with self.subTest(address=address), self.assertRaises(CredentialsError):
                self.manager.rotate('grafana', 'admin', 'new-monitor-password')
        self.assertEqual(self.http_calls, [])

    def test_grafana_subpath_is_preserved_without_following_public_redirects(self):
        self.containers['android-farm-grafana']['Config']['Env'].extend([
            'GF_SERVER_SERVE_FROM_SUB_PATH=true', 'GF_SERVER_ROOT_URL=https://farm.example.test/metrics/'])
        self.manager.rotate('grafana', 'admin', 'new-monitor-password')
        self.assertTrue(all(url.startswith('http://172.30.0.3:3000/metrics/api/')
                            for _, url, *_ in self.http_calls))

    def test_unverifiable_rollback_saves_private_recovery_without_false_success(self):
        original = self.manager._refresh

        def fail_refresh(plan):
            if plan == 'android-farm-grafana-secret-init':
                self.fail_all_http = True
                raise CredentialsError('injected failure')
            original(plan)

        self.manager._refresh = fail_refresh
        with self.assertRaises(CredentialsError) as failure:
            self.manager.rotate('grafana', 'new-operator', 'new-platform-password')
        self.assertIn('credential-recovery.json', str(failure.exception))
        self.assertNotIn('new-platform-password', str(failure.exception))
        path = self.config / 'credential-recovery.json'
        self.assertEqual(json.loads(path.read_text())['password'], 'new-platform-password')
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
