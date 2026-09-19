"""Run real read-only entrypoints without dirtying a manifested release."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from installer import install


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == 'posix' and os.geteuid() != 0,
                 'installed release verification requires root-owned files on Linux')
class ReleaseBytecodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.release, _, _ = install.install_release(
            ROOT, self.root / 'releases', catalog_count=1)
        self.cwd = self.root / 'unrelated-working-directory'
        self.cwd.mkdir()
        # The test must expose missing guards, even when the test runner itself
        # disables bytecode or redirects it to an external cache directory.
        self.environment = os.environ.copy()
        for name in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX',
                     'PYTHONPATH', 'PYTHONHOME'):
            self.environment.pop(name, None)
        self.assert_clean_release()

    def assert_clean_release(self):
        caches = [item.relative_to(self.release).as_posix()
                  for item in self.release.rglob('*')
                  if item.name == '__pycache__' or item.suffix in {'.pyc', '.pyo'}]
        self.assertEqual(caches, [])
        valid, detail = install.verify_release_directory(self.release)
        self.assertTrue(valid, detail)

    def test_cli_help_does_not_write_bytecode_without_inherited_guard(self):
        result = subprocess.run(
            [sys.executable, str(self.release / 'provisioner.py'), '--help'],
            cwd=self.cwd, env=self.environment, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage:', result.stdout)
        self.assertIn('diagnose', result.stdout)
        self.assert_clean_release()

    def test_cli_child_inherits_guard_without_importing_cli_as_a_module(self):
        # run_path executes the entrypoint without first creating its .pyc;
        # invoke then starts a fresh Python child through the real CLI helper.
        program = (
            'import os, runpy, sys; '
            'assert not sys.dont_write_bytecode; '
            'assert "PYTHONDONTWRITEBYTECODE" not in os.environ; '
            'sys.path.insert(0, sys.argv[1]); '
            'namespace = runpy.run_path(sys.argv[2]); '
            'namespace["invoke"]("farmctl.py", "--help")'
        )
        result = subprocess.run(
            [sys.executable, '-c', program, str(self.release),
             str(self.release / 'provisioner.py')],
            cwd=self.cwd, env=self.environment, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage: farmctl.py', result.stdout)
        self.assert_clean_release()

    def test_catalog_renderer_keeps_selected_release_clean(self):
        # A selected source may itself be an installed release. Rendering must
        # not add generate_farm/ops bytecode to that verified source tree.
        with patch.dict(os.environ, self.environment, clear=True):
            rendered = install._release_content(
                'docker-compose.farm.yml', self.release / 'docker-compose.farm.yml', 1)
        self.assertIn(b'android-num01', rendered)
        self.assert_clean_release()

    def test_verifier_rejects_unmanifested_bytecode(self):
        cache = self.release / 'ops' / '__pycache__'
        cache.mkdir()
        (cache / f'inventory.{sys.implementation.cache_tag}.pyc').write_bytes(
            b'unreviewed bytecode must never be accepted\n')
        valid, detail = install.verify_release_directory(self.release)
        self.assertFalse(valid)
        self.assertIn('release contains files outside its manifest', detail)


if __name__ == '__main__':
    unittest.main()
