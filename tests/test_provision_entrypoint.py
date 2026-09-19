"""Exercise the provisioning child entrypoint without Docker or host mutations."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops import provision


ROOT = Path(__file__).resolve().parents[1]


class ProvisionEntrypointTests(unittest.TestCase):
    def child(self, argument):
        # The caller may have any cwd; only the selected release may supply ops.
        script = (
            'import os, sys; '
            'sys.path.insert(0, sys.argv[1]); '
            'from ops.provision import run_farmctl; '
            'os.chdir(sys.argv[2]); '
            'run_farmctl("start", "num03", sys.argv[3])'
        )
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        with tempfile.TemporaryDirectory() as directory:
            decoy = Path(directory) / 'ops'
            decoy.mkdir()
            (decoy / '__init__.py').write_text(
                'raise RuntimeError("wrong release selected")\n', encoding='utf-8')
            return subprocess.run(
                [sys.executable, '-I', '-c', script, str(ROOT), directory, argument],
                cwd=directory, env=env, capture_output=True, text=True, timeout=30)

    def test_help_resolves_package_from_release_without_pythonpath(self):
        result = self.child('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage: farmctl.py', result.stdout)
        self.assertEqual(result.stderr, '')

    def test_child_failure_is_not_swallowed(self):
        result = self.child('--not-a-lifecycle-option')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CalledProcessError', result.stderr)
        self.assertNotIn('ImportError', result.stderr)
        self.assertNotIn('wrong release selected', result.stderr)

    def test_lifecycle_invocation_preserves_output_and_timeout(self):
        completed = subprocess.CompletedProcess([], 0)
        for action, timeout in (('start', 3300), ('stop', 600)):
            with self.subTest(action=action), patch(
                    'ops.provision.subprocess.run', return_value=completed) as runner:
                result = provision.run_farmctl(action, 'num03', timeout=timeout)
                runner.assert_called_once_with(
                    [sys.executable, '-m', 'ops.farmctl', action, 'num03'],
                    cwd=str(provision.ROOT), check=True, text=True, timeout=timeout)
                self.assertIs(result, completed)


if __name__ == '__main__':
    unittest.main()
