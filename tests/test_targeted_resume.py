"""Target selection cannot rebind or allocate a device during a continuation."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from ops import inventory
import provisioner


class TargetedAllocationTests(unittest.TestCase):
    def setUp(self):
        self.state = inventory.empty_inventory()
        self.first, _ = inventory.choose(self.state, 'phone-1', 'proxy-1', 'request-1')
        self.first['phase'] = 'ready_for_operator'
        self.second, _ = inventory.choose(self.state, 'phone-2', 'proxy-2', 'request-2')
        self.second['phase'] = 'failed'
        self.before = deepcopy(self.state)

    def test_matching_target_returns_same_record_without_allocation(self):
        record, created = inventory.choose(self.state, 'phone-2', 'proxy-2', 'request-2',
                                           resume_id='num02')
        self.assertIs(record, self.second)
        self.assertFalse(created)
        self.assertEqual(self.state, self.before)

    def test_target_must_match_original_phone_and_immutable_request(self):
        cases = (
            ('num02', 'phone-1', 'proxy-1', 'request-1', 'resume phone does not match'),
            ('num02', 'new-phone', 'proxy-2', 'request-2', 'resume phone does not match'),
            ('num02', 'phone-2', 'proxy-2', 'different-request', 'resume request differs'),
            ('num02', 'phone-2', 'different-proxy', 'request-2', 'resume request differs'),
            ('num03', 'phone-1', 'proxy-1', 'request-1', 'resume target is not allocated'),
            ('num03', 'new-phone', 'new-proxy', 'new-request', 'resume target is not allocated'),
        )
        for target, phone, proxy, request, error in cases:
            with self.subTest(target=target, phone=phone, request=request), \
                    self.assertRaisesRegex(RuntimeError, error):
                inventory.choose(self.state, phone, proxy, request, resume_id=target)
            self.assertEqual(self.state, self.before)

    def test_allocator_accepts_only_canonical_target_identifiers(self):
        for target in ('', 'num2', 'num002', 'dev02', 'num00', 'num8193', [], 2):
            with self.subTest(target=target), self.assertRaisesRegex(RuntimeError, 'canonical device identifier'):
                inventory.choose(self.state, 'phone-2', 'proxy-2', 'request-2', resume_id=target)
            self.assertEqual(self.state, self.before)

    def test_generic_allocation_and_resume_behavior_is_unchanged(self):
        record, created = inventory.choose(self.state, 'phone-2', 'proxy-2', 'request-2')
        self.assertIs(record, self.second)
        self.assertFalse(created)
        with self.assertRaisesRegex(RuntimeError, 'incomplete device before adding another'):
            inventory.choose(self.state, 'phone-3', 'proxy-3', 'request-3')
        self.assertEqual(self.state, self.before)
        self.second['phase'] = 'ready_for_operator'
        record, created = inventory.choose(self.state, 'phone-3', 'proxy-3', 'request-3')
        self.assertTrue(created)
        self.assertEqual(record['id'], 'num03')


class TargetedResumeCliTests(unittest.TestCase):
    def test_cli_canonicalizes_resume_alias(self):
        args = provisioner.build_parser().parse_args([
            'up', '--request', 'request.json', '--resume-id', 'dev4'])
        self.assertEqual(args.resume_id, 'num04')

    def test_cli_rejects_invalid_target_at_argument_boundary(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            provisioner.build_parser().parse_args([
                'up', '--request', 'request.json', '--resume-id', 'num00'])
        self.assertEqual(failure.exception.code, 2)

    def test_resume_id_cannot_be_combined_with_plain_start(self):
        stderr = io.StringIO()
        with patch.object(provisioner, 'require_host_root') as root_check, \
                patch.object(provisioner, 'load_config') as load, \
                patch.object(provisioner, 'invoke') as invoke, \
                redirect_stderr(stderr), self.assertRaises(SystemExit) as failure:
            provisioner.main(['up', '--id', 'num04', '--resume-id', 'num04'])
        self.assertEqual(failure.exception.code, 2)
        self.assertIn('--resume-id requires --request', stderr.getvalue())
        root_check.assert_not_called()
        load.assert_not_called()
        invoke.assert_not_called()

    def test_cli_forwards_target_only_for_explicit_resume(self):
        config = provisioner.Config(
            compose_file=Path('compose.json'), compose_env_file=None, compose_project='qa-test',
            secret_dir=Path('private-secrets'), backup_dir=Path('backups'),
            console_url='https://farm.example.com', apk_trust_file=Path('trust.json'))
        for target in (None, 'dev4'):
            with self.subTest(target=target), \
                    patch.object(provisioner, 'require_host_root'), \
                    patch.object(provisioner, 'load_config', return_value=config), \
                    patch.object(provisioner, 'invoke', return_value=subprocess.CompletedProcess(
                        [], 0, 'num04: synthetic result\n', '')) as invoke, \
                    redirect_stdout(io.StringIO()):
                provisioner.main(['up', '--request', 'request.json',
                                  *(('--resume-id', target) if target else ())])
                args = invoke.call_args.args
                self.assertEqual(args[:3], ('provision.py', '--request', Path('request.json')))
                if target:
                    self.assertEqual(args[args.index('--resume-id') + 1], 'num04')
                else:
                    self.assertNotIn('--resume-id', args)


if __name__ == '__main__':
    unittest.main()
