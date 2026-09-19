"""Installer credential persistence tests; fixtures are synthetic and never sent."""
from contextlib import ExitStack, contextmanager, nullcontext
import getpass
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings

from installer import install, quickstart, token_store
from installer.coolify_api import CoolifyError


URL = 'https://coolify.example.test'
OLD_TOKEN = 'fixture-old-token-only'
NEW_TOKEN = 'fixture-new-token-only'


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'private' / 'coolify-credential.json'

    def write_payload(self, value):
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.path.chmod(0o600)

    def test_roundtrip_accepts_equivalent_api_path_and_keeps_url_binding(self):
        self.assertEqual(token_store.save(URL, OLD_TOKEN, path=self.path), self.path)
        self.assertEqual(token_store.load(URL + '/api/v1/', path=self.path), OLD_TOKEN)
        self.assertEqual(json.loads(self.path.read_text())['url'], URL + '/api/v1')
        for changed in ('https://other.example.test', URL + ':8443', 'http://127.0.0.1:8000'):
            with self.subTest(url=changed), self.assertRaisesRegex(RuntimeError, '--remember-token'):
                token_store.load(changed, path=self.path)

    def test_missing_credential_is_not_created_by_read(self):
        self.assertIsNone(token_store.load(URL, path=self.path))
        self.assertFalse(self.path.parent.exists())

    def test_invalid_tokens_are_rejected_without_echo(self):
        for token in ('', None, 42, 'fixture secret', 'fixture\nsecret', '\u0631\u0645\u0632', 'x' * 4097):
            with self.subTest(kind=type(token).__name__), self.assertRaises(ValueError) as raised:
                token_store.validate_token(token)
            if isinstance(token, str) and token:
                self.assertNotIn(token, str(raised.exception))

    def test_invalid_replacement_preserves_existing_bytes(self):
        token_store.save(URL, OLD_TOKEN, path=self.path)
        before = self.path.read_bytes()
        for url, token in ((URL, 'invalid fixture'), ('http://public.example.test', NEW_TOKEN)):
            with self.subTest(url=url), self.assertRaises((ValueError, RuntimeError)):
                token_store.save(url, token, path=self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replace_failure_preserves_existing_bytes(self):
        token_store.save(URL, OLD_TOKEN, path=self.path)
        before = self.path.read_bytes()
        with patch('ops.secureio.os.replace', side_effect=OSError('fixture replacement failure')):
            with self.assertRaises(OSError):
                token_store.save(URL, NEW_TOKEN, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_successful_replacement_overwrites_one_credential(self):
        token_store.save(URL, OLD_TOKEN, path=self.path)
        token_store.save(URL, NEW_TOKEN, path=self.path)
        self.assertEqual(token_store.load(URL, path=self.path), NEW_TOKEN)
        self.assertNotIn(OLD_TOKEN, self.path.read_text())

    def test_maximum_length_escaped_token_remains_readable(self):
        token = ('\\"' * 2048)
        token_store.save(URL, token, path=self.path)
        self.assertLessEqual(self.path.stat().st_size, token_store.MAX_FILE_BYTES)
        self.assertEqual(token_store.load(URL, path=self.path), token)

    def test_invalid_schema_is_not_echoed(self):
        valid = {'schema_version': 1, 'url': URL, 'token': OLD_TOKEN}
        for value in ([], {}, {**valid, 'schema_version': True}, {**valid, 'schema_version': 2},
                      {**valid, 'unexpected': OLD_TOKEN}, {**valid, 'url': 42},
                      {**valid, 'token': ['fixture-token']}):
            self.write_payload(value)
            with self.subTest(value_type=type(value).__name__), self.assertRaises((RuntimeError, ValueError)) as raised:
                token_store.load(URL, path=self.path)
            self.assertNotIn(OLD_TOKEN, str(raised.exception))

    def test_malformed_and_oversized_content_is_not_echoed(self):
        self.write_payload({})
        for text in ('{"token":"' + OLD_TOKEN, OLD_TOKEN * token_store.MAX_FILE_BYTES):
            self.path.write_text(text, encoding='utf-8')
            with self.assertRaises(RuntimeError) as raised:
                token_store.load(URL, path=self.path)
            self.assertNotIn(OLD_TOKEN, str(raised.exception))

    def make_symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest('Platform cannot create symlink fixtures')

    def test_dangling_symlink_is_never_treated_as_missing(self):
        self.path.parent.mkdir(mode=0o700)
        self.make_symlink(self.path, self.root / 'missing')
        for operation in (lambda: token_store.load(URL, path=self.path),
                          lambda: token_store.save(URL, NEW_TOKEN, path=self.path)):
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                operation()
        self.assertFalse((self.root / 'missing').exists())

    def test_ancestor_symlink_is_rejected(self):
        target = self.root / 'real'
        target.mkdir(mode=0o700)
        linked = self.root / 'linked'
        self.make_symlink(linked, target, directory=True)
        path = linked / 'nested' / 'credential.json'
        for operation in (lambda: token_store.load(URL, path=path),
                          lambda: token_store.save(URL, NEW_TOKEN, path=path)):
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                operation()
        self.assertFalse((target / 'nested').exists())

    @unittest.skipUnless(os.name == 'posix', 'Requires POSIX ownership and modes')
    def test_private_file_and_parent_modes_are_enforced(self):
        token_store.save(URL, OLD_TOKEN, path=self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            token_store.load(URL, path=self.path)
        with self.assertRaises(RuntimeError):
            token_store.save(URL, NEW_TOKEN, path=self.path)
        self.path.chmod(0o600)
        self.path.parent.chmod(0o755)
        with self.assertRaises(RuntimeError):
            token_store.load(URL, path=self.path)
        with self.assertRaises(RuntimeError):
            token_store.save(URL, NEW_TOKEN, path=self.path)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)


class TokenSelectionTests(unittest.TestCase):
    def test_absent_store_prompts_without_persisting(self):
        with patch.object(token_store, 'load', return_value=None), \
             patch.object(token_store, 'save') as save, \
             patch.object(quickstart.getpass, 'getpass', return_value=NEW_TOKEN) as prompt:
            self.assertEqual(quickstart.select_token(URL), (NEW_TOKEN, 'prompt'))
            prompt.assert_called_once()
            save.assert_not_called()

    def test_saved_token_skips_prompt(self):
        with patch.object(token_store, 'load', return_value=OLD_TOKEN), \
             patch.object(quickstart.getpass, 'getpass') as prompt, \
             patch.object(quickstart.sys, 'stdout', io.StringIO()) as output:
            self.assertEqual(quickstart.select_token(URL), (OLD_TOKEN, 'saved'))
            prompt.assert_not_called()
            self.assertNotIn(OLD_TOKEN, output.getvalue())

    def test_explicit_private_file_takes_precedence_even_when_remembering(self):
        for remember in (False, True):
            with self.subTest(remember=remember), \
                 patch.object(install, '_private_password', return_value=NEW_TOKEN) as read, \
                 patch.object(token_store, 'load') as load, \
                 patch.object(quickstart.getpass, 'getpass') as prompt:
                path = Path('fixture-token-file')
                self.assertEqual(quickstart.select_token(URL, token_file=path, remember=remember),
                                 (NEW_TOKEN, 'file'))
                read.assert_called_once_with(path)
                load.assert_not_called()
                prompt.assert_not_called()

    def test_remember_flag_requests_fresh_token_without_loading_old_entry(self):
        with patch.object(token_store, 'load') as load, \
             patch.object(token_store, 'save') as save, \
             patch.object(quickstart.getpass, 'getpass', return_value=NEW_TOKEN) as prompt:
            self.assertEqual(quickstart.select_token(URL, remember=True), (NEW_TOKEN, 'prompt'))
            load.assert_not_called()
            save.assert_not_called()
            prompt.assert_called_once()

    def test_unsafe_saved_entry_does_not_silently_fall_back_to_prompt(self):
        with patch.object(token_store, 'load', side_effect=RuntimeError('fixture unsafe store')), \
             patch.object(quickstart.getpass, 'getpass') as prompt:
            with self.assertRaises(RuntimeError):
                quickstart.select_token(URL)
            prompt.assert_not_called()

    def test_echo_fallback_warning_aborts_prompt(self):
        def fallback(_label):
            warnings.warn('fixture terminal cannot hide input', getpass.GetPassWarning)
            self.fail('Input must not be read after an echo fallback warning')

        with patch.object(token_store, 'load', return_value=None), \
             patch.object(quickstart.getpass, 'getpass', side_effect=fallback):
            with self.assertRaisesRegex(RuntimeError, '--token-file'):
                quickstart.select_token(URL)


class InstallerTokenIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'private' / 'credential.json'

    @contextmanager
    def mocked_main(self, *, server_error=None):
        with ExitStack() as stack:
            output = io.StringIO()
            stack.enter_context(patch.object(quickstart.sys, 'stdout', output))
            stack.enter_context(patch.object(quickstart.sys, 'stderr', io.StringIO()))
            stack.enter_context(patch.object(quickstart, 'require_host'))
            stack.enter_context(patch.object(quickstart, 'setup_lock', return_value=nullcontext()))
            stack.enter_context(patch.object(quickstart, 'load_state', return_value={}))
            state = stack.enter_context(patch.object(quickstart, 'save_state'))
            stack.enter_context(patch.object(install, '_read_json_if_regular', return_value={}))
            stack.enter_context(patch.object(quickstart, 'source_commit', return_value='a' * 40))
            stack.enter_context(patch.object(quickstart.getpass, 'getpass', return_value=NEW_TOKEN))
            stack.enter_context(patch.object(token_store, 'CREDENTIAL_FILE', self.path))
            client = stack.enter_context(patch('installer.coolify_api.CoolifyClient'))
            stack.enter_context(patch.object(quickstart, 'select_server', return_value='fixture-server',
                                            side_effect=server_error))
            stack.enter_context(patch.object(quickstart, 'prepare_auth', return_value=(None, None)))
            run = stack.enter_context(patch.object(quickstart, 'run_setup',
                                                  side_effect=RuntimeError('fixture later setup failure')))
            yield output, state, client, run

    def arguments(self, *extra):
        return ['--domain', 'farm.example.test', '--coolify-url', URL, *extra]

    def test_without_opt_in_main_does_not_save_prompted_token(self):
        with self.mocked_main() as (output, state, _client, run):
            self.assertEqual(quickstart.main(self.arguments()), 1)
            run.assert_called_once()
            self.assertFalse(self.path.exists())
            self.assertNotIn(NEW_TOKEN, output.getvalue())
            self.assertNotIn(NEW_TOKEN, repr(state.call_args_list))

    def test_validated_token_is_kept_when_a_later_install_step_fails(self):
        with self.mocked_main() as (output, state, _client, run):
            self.assertEqual(quickstart.main(self.arguments('--remember-token')), 1)
            run.assert_called_once()
            self.assertEqual(token_store.load(URL, path=self.path), NEW_TOKEN)
            self.assertNotIn(NEW_TOKEN, output.getvalue())
            self.assertNotIn(NEW_TOKEN, repr(state.call_args_list))

    def test_auth_or_connection_failure_does_not_replace_stored_bytes(self):
        token_store.save(URL, OLD_TOKEN, path=self.path)
        before = self.path.read_bytes()
        for error in (CoolifyError('fixture denied', status=401), CoolifyError('fixture forbidden', status=403),
                      CoolifyError('fixture timeout')):
            with self.subTest(error=str(error)), self.mocked_main(server_error=error) as (output, state, _client, run):
                self.assertEqual(quickstart.main(self.arguments('--remember-token')), 1)
                self.assertEqual(self.path.read_bytes(), before)
                run.assert_not_called()
                state.assert_not_called()
                self.assertNotIn(OLD_TOKEN, output.getvalue())
                self.assertNotIn(NEW_TOKEN, output.getvalue())

    def test_saved_token_rejection_gives_renewal_hint_without_erasing_file(self):
        token_store.save(URL, OLD_TOKEN, path=self.path)
        before = self.path.read_bytes()
        with self.mocked_main(server_error=CoolifyError('fixture denied', status=401)) as (output, _, client, run):
            self.assertEqual(quickstart.main(self.arguments()), 1)
            client.assert_called_once_with(URL, OLD_TOKEN)
            run.assert_not_called()
            self.assertIn('--remember-token', output.getvalue())
            self.assertNotIn(OLD_TOKEN, output.getvalue())
            self.assertEqual(self.path.read_bytes(), before)

    def test_mismatched_url_is_rejected_before_constructing_api_client(self):
        token_store.save('https://other.example.test', OLD_TOKEN, path=self.path)
        with self.mocked_main() as (output, _, client, run):
            self.assertEqual(quickstart.main(self.arguments()), 1)
            client.assert_not_called()
            run.assert_not_called()
            self.assertNotIn(OLD_TOKEN, output.getvalue())

    def test_maintenance_neither_reads_nor_prompts_nor_saves_credentials(self):
        for operation in ('--diagnose', '--repair-control-plane'):
            with self.subTest(operation=operation), \
                 patch.object(quickstart, 'require_host'), \
                 patch.object(quickstart, 'setup_lock', return_value=nullcontext()), \
                 patch.object(quickstart, 'diagnose', return_value={'ready': True}), \
                 patch.object(quickstart, 'repair_control_plane', return_value={'ready': True}), \
                 patch.object(quickstart, 'select_token') as select, \
                 patch.object(token_store, 'load') as load, \
                 patch.object(token_store, 'save') as save, \
                 patch.object(quickstart.sys, 'stdout', io.StringIO()):
                self.assertEqual(quickstart.main([operation]), 0)
                select.assert_not_called()
                load.assert_not_called()
                save.assert_not_called()
                self.assertEqual(quickstart.main([operation, '--remember-token']), 1)
                select.assert_not_called()
                save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
