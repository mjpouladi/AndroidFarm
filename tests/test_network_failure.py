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
                patch.object(farmctl, 'network_failure_evidence') as evidence:
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
                patch.object(farmctl, 'network_failure_evidence') as evidence, redirect_stderr(output):
            with self.assertRaisesRegex(RuntimeError, 'after-android-boot') as failure:
                farmctl.wait_proxy('num04', timeout=1, phase='after-android-boot')
        evidence.assert_called_once_with('num04')
        self.assertIn('healthcheck exit 7', output.getvalue())
        self.assertIn('fixture-private-network-detail', output.getvalue())
        self.assertNotIn('fixture-private-network-detail', str(failure.exception))
        self.assertIn('proxy did not become healthy before timeout', str(failure.exception))

    def test_docker_exec_timeout_still_reports_evidence(self):
        with patch.object(farmctl.time, 'monotonic', side_effect=[0, 0, 2]), \
                patch.object(farmctl.time, 'sleep'), \
                patch.object(farmctl.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['docker'], 15)), \
                patch.object(farmctl, 'network_failure_evidence') as evidence, redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(RuntimeError):
                farmctl.wait_proxy('num04', timeout=1)
        evidence.assert_called_once_with('num04')
        self.assertIn('healthcheck execution timed out', output.getvalue())

    def test_evidence_is_read_only_bounded_and_best_effort(self):
        with patch.object(farmctl.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, 'x' * 10000, ''),
                OSError('fixture unavailable'), subprocess.TimeoutExpired(['docker'], 3),
                subprocess.CompletedProcess([], 0, '-P OUTPUT ACCEPT', ''),
                subprocess.CompletedProcess([], 0, '-A FARM4 -j RETURN', '')]) as run, \
                redirect_stderr(io.StringIO()) as output:
            farmctl.network_failure_evidence('num04')
        self.assertEqual(run.call_count, 5)
        self.assertLess(len(output.getvalue()), 6000)
        self.assertIn('diagnostic command unavailable or timed out', output.getvalue())
        for call in run.call_args_list:
            self.assertLessEqual(call.kwargs['timeout'], 3)
            self.assertEqual(call.kwargs['errors'], 'replace')
            self.assertNotIn('shell', call.kwargs)
            self.assertFalse({'-F', '-A', '-I', 'restart', 'stop', 'start', 'flush'} & set(call.args[0]))


if __name__ == '__main__':
    unittest.main()
