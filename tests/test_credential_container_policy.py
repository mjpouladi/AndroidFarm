"""Regression coverage for Docker's real inspect capability representation."""
import copy
import os
import unittest

from tests import test_api_credentials as fixtures
from services.api.credentials import CredentialsError


@unittest.skipUnless(os.name != 'posix' or os.geteuid() == 0, 'managed credential files require root')
class CredentialContainerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CredentialManagerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_live_docker_cap_names_allow_platform_rotation(self):
        for name in ('android-farm-gateway-secret-init', 'android-farm-grafana-secret-init'):
            self.fixture.containers[name]['HostConfig']['CapAdd'] = [
                'CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_FOWNER']
        result = self.fixture.manager.rotate('platform', 'new-operator', 'new-platform-password')
        self.assertEqual(result['changed'], ['web', 'grafana'])
        self.assertEqual(self.fixture.current_user, 'new-operator')
        started = [argv[-1] for argv, _ in self.fixture.commands if argv[:2] == ['docker', 'start']]
        self.assertEqual(started, ['android-farm-grafana-secret-init', 'android-farm-gateway-secret-init'])

    def test_capability_aliases_and_no_new_privileges_forms_preserve_policy(self):
        host = self.fixture.containers['android-farm-gateway-secret-init']['HostConfig']
        host['CapAdd'] = ['CAP_CHOWN', 'dac_override', 'CAP_FOWNER']
        for security in ('no-new-privileges', 'no-new-privileges:true', 'no-new-privileges=true'):
            host['SecurityOpt'] = [security]
            with self.subTest(security=security):
                result = self.fixture.manager.rotate('web', 'new-operator', 'new-platform-password')
                self.assertEqual(result['changed'], ['web'])

    def test_extra_missing_malformed_or_weakened_capabilities_fail_before_any_change(self):
        name = 'android-farm-gateway-secret-init'
        baseline = copy.deepcopy(self.fixture.containers[name]['HostConfig'])
        before = self.fixture.files()
        mutations = [
            {'CapAdd': ['CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_FOWNER', 'CAP_SYS_ADMIN']},
            {'CapAdd': ['CAP_CHOWN', 'CAP_DAC_OVERRIDE']},
            {'CapAdd': ['ALL']},
            {'CapAdd': ['CAP_CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_FOWNER']},
            {'CapAdd': [' CAP_CHOWN', 'CAP_DAC_OVERRIDE', 'CAP_FOWNER']},
            {'CapAdd': ['CAP_CHOWN', 'CAP_DAC_OVERRIDE', {}]},
            {'CapAdd': {'CAP_CHOWN': True, 'CAP_DAC_OVERRIDE': True, 'CAP_FOWNER': True}},
            {'CapDrop': []}, {'CapDrop': None}, {'CapDrop': ['CAP_ALL']},
            {'SecurityOpt': ['no-new-privileges=false']},
            {'SecurityOpt': ['no-new-privileges:true', 'seccomp=unconfined']},
            {'SecurityOpt': {'no-new-privileges:true': True}},
        ]
        for mutation in mutations:
            self.fixture.containers[name]['HostConfig'] = dict(baseline, **mutation)
            with self.subTest(mutation=mutation), self.assertRaises(CredentialsError):
                self.fixture.manager.rotate('platform', 'new-operator', 'new-platform-password')
            self.assertEqual(self.fixture.files(), before)
            self.assertEqual(self.fixture.http_calls, [])
            self.assertFalse(any(argv[:2] == ['docker', 'start'] for argv, _ in self.fixture.commands))


if __name__ == '__main__':
    unittest.main()
