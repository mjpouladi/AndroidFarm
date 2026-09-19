import json
import subprocess
import unittest
from unittest.mock import Mock

from services.api.components import Components, ComponentError, UNITS, CONTAINERS


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.units = {name: {'Id': name, 'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running'}
                      for name in UNITS}
        self.containers = {name: {'Name': '/' + name, 'Id': 'safe-id-' + name, 'Config': {'Labels': {
            'farm.stack': 'core', 'farm.role': role, 'com.docker.compose.project': 'android-farm-core'}},
            'State': {'Running': True, 'Health': {'Status': 'healthy'}}}
            for name, (role, _) in CONTAINERS.items()}

        def run(argv, **_):
            if argv[:2] == ['systemctl', 'show']:
                value = '\n\n'.join('\n'.join(f'{k}={v}' for k, v in row.items()) for row in self.units.values())
            elif argv[:2] == ['docker', 'inspect']:
                value = json.dumps(list(self.containers.values()))
            else:
                value = ''
            return subprocess.CompletedProcess(argv, 0, value, '')

        self.runner = Mock(side_effect=run)
        self.components = Components(runner=self.runner)

    def test_health_is_observed_and_not_guessed_from_a_name(self):
        self.units['redis-server.service']['ActiveState'] = 'failed'
        self.containers['android-farm-gateway']['State']['Health']['Status'] = 'unhealthy'
        self.containers['farm-console']['Config']['Labels']['com.docker.compose.project'] = 'other-app'
        values = {row['id']: row for row in self.components.snapshot()}
        self.assertEqual(values['redis-server.service']['state'], 'failed')
        self.assertEqual(values['android-farm-gateway']['state'], 'failed')
        self.assertEqual(values['farm-console']['state'], 'unknown')
        self.assertEqual(values['android-farm-grafana']['state'], 'active')

    def test_activation_starts_only_existing_stopped_central_containers(self):
        self.containers['android-farm-gateway']['State']['Running'] = False
        result = self.components.activate()
        self.assertEqual(result['started_containers'], ['android-farm-gateway'])
        docker_starts = [call.args[0] for call in self.runner.call_args_list if call.args[0][:2] == ['docker', 'start']]
        self.assertEqual(docker_starts, [['docker', 'start', 'safe-id-android-farm-gateway']])
        self.assertTrue(all('num01' not in str(call.args) for call in self.runner.call_args_list))

    def test_foreign_or_missing_component_blocks_all_mutations(self):
        self.containers['farm-console']['Config']['Labels']['farm.stack'] = 'foreign'
        with self.assertRaises(ComponentError):
            self.components.activate()
        self.assertTrue(all(call.args[0][1] in {'show', 'inspect'} for call in self.runner.call_args_list))

    def test_unavailable_daemons_are_unknown_not_falsely_active(self):
        self.runner.side_effect = OSError('private information never goes to the UI')
        values = self.components.snapshot()
        self.assertEqual(len(values), len(UNITS) + len(CONTAINERS))
        self.assertTrue(all(row['state'] == 'unknown' for row in values))
        self.assertNotIn('private information', json.dumps(values))
