"""Retain failed network evidence without changing routing or exposing it via API."""
from contextlib import redirect_stderr
import io
import subprocess
import unittest
from unittest.mock import patch

from ops import farmctl


class NetworkFailureTests(unittest.TestCase):
    def test_success_does_not_collect_evidence_or_change_network(self):
        with patch.object(farmctl.time, 'monotonic', side_effect=[0, 0]), \
                patch.object(farmctl.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
                patch.object(farmctl, 'network_forensics') as evidence:
            farmctl.wait_proxy('num04', timeout=1, phase='after-android-boot')
        self.assertEqual(run.call_args.args[0], ['docker', 'exec', 'proxy-num04', '/healthcheck.sh'])
        run.assert_called_once()
        evidence.assert_not_called()

    def test_timeout_keeps_curl_reason_in_host_output_but_not_public_exception(self):
        output = io.StringIO()
        with patch.object(farmctl.time, 'monotonic', side_effect=[0, 0, 2]), \
                patch.object(farmctl.time, 'sleep'), \
                patch.object(farmctl.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 7, '', 'curl: (7) fixture-private-network-detail')), \
                patch.object(farmctl, 'network_forensics') as evidence, redirect_stderr(output):
            with self.assertRaisesRegex(RuntimeError, 'after-android-boot') as failure:
                farmctl.wait_proxy('num04', timeout=1, phase='after-android-boot')
        # The lifecycle failure handler collects evidence once, before cleanup.
        evidence.assert_not_called()
        self.assertIn('healthcheck exit 7', output.getvalue())
        self.assertIn('fixture-private-network-detail', output.getvalue())
        self.assertNotIn('fixture-private-network-detail', str(failure.exception))
        self.assertIn('proxy did not become healthy before timeout', str(failure.exception))

    def test_docker_exec_timeout_preserves_health_reason_without_duplicate_forensics(self):
        with patch.object(farmctl.time, 'monotonic', side_effect=[0, 0, 2]), \
                patch.object(farmctl.time, 'sleep'), \
                patch.object(farmctl.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['docker'], 15)), \
                patch.object(farmctl, 'network_forensics') as evidence, redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(RuntimeError):
                farmctl.wait_proxy('num04', timeout=1)
        evidence.assert_not_called()
        self.assertIn('healthcheck execution timed out', output.getvalue())

    def test_evidence_is_read_only_bounded_and_best_effort(self):
        results = [
                subprocess.CompletedProcess([], 0, 'x' * 10000, ''),
                OSError('fixture unavailable'), subprocess.TimeoutExpired(['docker'], 3),
                *[subprocess.CompletedProcess([], 0, 'x' * 10000, '') for _ in range(10)]]
        with patch.object(farmctl, 'inspect', return_value={'State': {'Running': True}}), \
                patch.object(farmctl.subprocess, 'run', side_effect=results) as run, \
                redirect_stderr(io.StringIO()) as output:
            farmctl.network_forensics('num04')
        self.assertEqual(run.call_count, 13)
        self.assertLess(len(output.getvalue()), 34 * 1024)
        self.assertIn('fixture unavailable', output.getvalue())
        self.assertIn('timed out', output.getvalue())
        for label in ('resolv.conf', 'addresses', 'rules', 'routes', 'filter', 'nat', 'dns',
                      'https-by-name', 'https-by-ip', 'host-docker-user', 'host-guard',
                      'host-forward-policy', 'host-bridge-route'):
            self.assertIn(f'[{label}]', output.getvalue())
        for call in run.call_args_list:
            self.assertLessEqual(call.kwargs['timeout'], 3)
            self.assertEqual(call.kwargs['errors'], 'replace')
            self.assertNotIn('shell', call.kwargs)
            self.assertFalse({'-F', '-A', '-I', 'restart', 'stop', 'start', 'flush'} & set(call.args[0]))
            self.assertFalse({'env', 'printenv', '--env', '-e'} & set(call.args[0]))

    def test_stopped_namespace_collects_only_host_forensics(self):
        with patch.object(farmctl, 'inspect', return_value={'State': {'Running': False}}), \
                patch.object(farmctl.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, 'fixture host evidence', '')) as run, redirect_stderr(io.StringIO()) as output:
            farmctl.network_forensics('num04')
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all(call.args[0][0] != 'docker' for call in run.call_args_list))
        self.assertIn('namespace evidence unavailable', output.getvalue())

    def test_inspect_accepts_a_bounded_timeout_without_changing_default_callers(self):
        with patch.object(farmctl.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, '[{"State": {"Running": true}}]', '')) as run:
            self.assertEqual(farmctl.inspect('proxy-num04', timeout=3), {'State': {'Running': True}})
            self.assertEqual(run.call_args.kwargs['timeout'], 3)
            self.assertEqual(run.call_args.kwargs['errors'], 'replace')
            farmctl.inspect('proxy-num04')
            self.assertIsNone(run.call_args.kwargs['timeout'])

    def test_inspection_failure_retains_bounded_host_evidence_without_claiming_stopped(self):
        for failure in (OSError('fixture unavailable'), subprocess.TimeoutExpired(['docker'], 3),
                        ValueError('fixture invalid inspection JSON')):
            with self.subTest(error=type(failure).__name__), \
                    patch.object(farmctl, 'inspect', side_effect=failure) as inspect, \
                    patch.object(farmctl.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, 'fixture host evidence', '')) as run, redirect_stderr(io.StringIO()) as output:
                farmctl.network_forensics('num04')
            inspect.assert_called_once_with('proxy-num04', timeout=3)
            self.assertEqual(run.call_count, 4)
            self.assertTrue(all(call.args[0][0] != 'docker' for call in run.call_args_list))
            self.assertIn('proxy state unavailable or not running', output.getvalue())
            self.assertIn('[host-guard]', output.getvalue())


if __name__ == '__main__':
    unittest.main()
