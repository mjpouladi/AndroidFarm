import json
from pathlib import Path
import subprocess
import unittest

from ops.healthcheck import (RECOVERY_TIMEOUT, Observation, canonical_device, compose_argv,
                             observe, prometheus, reconcile)


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


class HealthcheckTests(unittest.TestCase):
    def config(self):
        return {"compose_file": Path("/opt/android-farm/release/docker-compose.farm.yml"),
                "compose_project": "android-farm-runtime", "compose_env_file": None,
                "secret_dir": Path("/etc/android-farm/secrets"),
                "profile_dir": Path("/etc/android-farm/device-profiles"),
                "proxy_registry": Path("/var/lib/android-farm/proxies.json"),
                "proxy_store_dir": Path("/etc/android-farm/proxies")}

    def test_recovery_uses_guarded_farmctl_action(self):
        argv = compose_argv(self.config(), "restart", "android", "num01")
        self.assertEqual(argv[:5], [argv[0], "-m", "ops.farmctl", "recover", "num01"])
        self.assertNotIn("restart", argv)
        screen = compose_argv(self.config(), "restart", "screen", "num01")
        self.assertEqual(screen[3:5], ["recover-screen", "num01"])
        with self.assertRaises(ValueError):
            compose_argv(self.config(), "restart", "proxy", "num01")
        with self.assertRaises(ValueError):
            canonical_device("num01;rm")

    def test_stopped_device_is_not_probed_or_restarted(self):
        calls = []

        def runner(argv, timeout=30):
            calls.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                name = argv[-1]
                running = name != "android-num01" and False
                return completed(argv, stdout=json.dumps([{"State": {"Running": running}}]))
            raise AssertionError("stopped device must not run exec probes")

        item = observe("num01", runner=runner, clock=lambda: 1000)
        self.assertEqual(item.state, "stopped")
        self.assertEqual(len(calls), 3)
        state = {"schema_version": 1, "devices": {}}
        actions = reconcile([item], self.config(), state, runner=lambda *_a, **_k: self.fail(),
                            clock=lambda: 1000)
        self.assertEqual(actions, [])

    def test_healthy_device_checks_proxy_shell_and_boot(self):
        def runner(argv, timeout=30):
            if argv[:2] == ["docker", "inspect"]:
                return completed(argv, stdout=json.dumps([{
                    "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"}
                }]))
            if argv[-1] == "/healthcheck.sh":
                return completed(argv)
            if argv[-3:] == ["shell", "echo", "android-farm-health"]:
                return completed(argv, stdout="android-farm-health\n")
            if argv[-3:] == ["shell", "getprop", "sys.boot_completed"]:
                return completed(argv, stdout="1\n")
            raise AssertionError(argv)

        item = observe("num01", runner=runner, clock=lambda: 1800000000)
        self.assertEqual(item.state, "healthy")
        self.assertTrue(item.adb_healthy)
        self.assertTrue(item.boot_completed)

    def test_adb_timeout_is_stalled_and_proxy_timeout_is_not_restarted(self):
        def runner(argv, timeout=30):
            if argv[:2] == ["docker", "inspect"]:
                return completed(argv, stdout=json.dumps([{
                    "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"}
                }]))
            if argv[-1] == "/healthcheck.sh":
                return completed(argv)
            raise subprocess.TimeoutExpired(argv, timeout)

        item = observe("num01", runner=runner, clock=lambda: 1800000000)
        self.assertEqual(item.state, "stalled")
        self.assertEqual(item.recovery_component, "android")
        self.assertTrue(item.android_running)

        def proxy_timeout(argv, timeout=30):
            if argv[-1] == "/healthcheck.sh":
                raise subprocess.TimeoutExpired(argv, timeout)
            return runner(argv, timeout)

        item = observe("num01", runner=proxy_timeout, clock=lambda: 1800000000)
        self.assertEqual(item.state, "proxy_unhealthy")
        self.assertIsNone(item.recovery_component)

    def test_inspection_timeout_exports_unknown_state_without_recovery(self):
        def runner(argv, timeout=30):
            raise subprocess.TimeoutExpired(argv, timeout)

        item = observe("num01", runner=runner)
        self.assertEqual(item.state, "probe_failed")
        self.assertIsNone(item.recovery_component)
        output = prometheus([item], {"devices": {}}, run_deadline=9000)
        self.assertIn('android_farm_probe_failed{device="num01"} 1', output)
        self.assertIn('android_farm_health_run_deadline_seconds 9000', output)
        unavailable = observe("num02", runner=lambda argv, timeout: completed(
            argv, code=1, stderr="Cannot connect to the Docker daemon"))
        self.assertTrue(unavailable.probe_failed)
        self.assertIsNone(unavailable.recovery_component)

    def test_cold_recovery_has_time_for_reviewed_image_build_and_boot(self):
        item = Observation("num01", "stalled", True, True, True, False, False, True, .2, "android")
        state = {"devices": {"num01": {"consecutive_failures": 1}}}
        timeouts = []
        reconcile([item], self.config(), state,
                  runner=lambda argv, timeout: timeouts.append(timeout) or completed(argv),
                  clock=lambda: 1000)
        self.assertEqual(timeouts, [RECOVERY_TIMEOUT])
        self.assertGreater(RECOVERY_TIMEOUT, 1200 + 300)

    def test_stall_recovery_has_limit_and_cooldown(self):
        observations = [
            Observation("num01", "stalled", True, True, True, False, False, True, .2, "android"),
            Observation("num02", "stalled", True, True, True, False, False, True, .3, "android"),
        ]
        state = {"schema_version": 1, "devices": {}}
        state["devices"] = {"num01": {"consecutive_failures": 1},
                            "num02": {"consecutive_failures": 1}}
        actions = reconcile(observations, self.config(), state, clock=lambda: 1000,
                            dry_run=True, restart_limit=1)
        self.assertEqual([action["device"] for action in actions], ["num01"])

        state["devices"]["num01"]["next_restart_at"] = 2000
        calls = []
        actions = reconcile([observations[0]], self.config(), state,
                            runner=lambda argv, timeout: calls.append(argv) or completed(argv),
                            clock=lambda: 1500)
        self.assertEqual(actions, [])
        self.assertEqual(calls, [])

    def test_screen_failure_restarts_existing_screen_and_metrics_are_labelled(self):
        item = Observation("num03", "stalled", True, False, True, False, False, True, .5, "screen")
        state = {"schema_version": 1, "devices": {}}
        calls = []
        reconcile([item], self.config(), state,
                  runner=lambda argv, timeout: calls.append(argv) or completed(argv),
                  clock=lambda: 2000)
        self.assertEqual(calls[0][3:5], ["recover-screen", "num03"])
        output = prometheus([item], state)
        self.assertIn('android_farm_adb_healthy{device="num03"} 0', output)
        self.assertIn('android_farm_proxy_healthcheck_duration_seconds{device="num03"} 0.500000', output)

    def test_failed_recovery_keeps_metrics_and_applies_cooldown(self):
        item = Observation("num04", "stalled", True, True, True, False, False, True, .4, "android")
        state = {"schema_version": 1, "devices": {"num04": {"consecutive_failures": 1}}}
        actions = reconcile([item], self.config(), state,
                            runner=lambda argv, timeout: completed(argv, code=1),
                            clock=lambda: 3000)
        self.assertFalse(actions[0]["succeeded"])
        record = state["devices"]["num04"]
        self.assertGreater(record["next_restart_at"], 3000)
        self.assertEqual(record["recovery_failure_total"], 1)
        self.assertIn('android_farm_health_recovery_failures_total{device="num04"} 1',
                      prometheus([item], state))

    def test_locked_recovery_skip_is_not_counted_as_restart_or_failure(self):
        item = Observation("num06", "stalled", True, True, True, False, False, True, .4, "android")
        state = {"schema_version": 1, "devices": {"num06": {"consecutive_failures": 1}}}
        actions = reconcile(
            [item], self.config(), state,
            runner=lambda argv, timeout: completed(
                argv, stdout="ANDROID_FARM_RECOVERY_SKIPPED num06 android-not-running\n"),
            clock=lambda: 5000,
        )
        self.assertTrue(actions[0]["skipped"])
        record = state["devices"]["num06"]
        self.assertEqual(record["consecutive_failures"], 0)
        self.assertNotIn("restart_total", record)
        self.assertNotIn("recovery_failure_total", record)
        self.assertNotIn("next_restart_at", record)

    def test_missing_screen_is_reported_without_unsafe_recreation(self):
        def runner(argv, timeout=30):
            if argv[:2] == ["docker", "inspect"]:
                if argv[-1] == "screen-num05":
                    return completed(argv, code=1, stderr="Error: No such object: screen-num05")
                return completed(argv, stdout=json.dumps([{
                    "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"}
                }]))
            if argv[-1] == "/healthcheck.sh":
                return completed(argv)
            raise AssertionError(argv)

        item = observe("num05", runner=runner, clock=lambda: 1800000000)
        self.assertEqual(item.state, "screen_missing")
        self.assertIsNone(item.recovery_component)
        state = {"schema_version": 1, "devices": {}}
        self.assertEqual(reconcile([item], self.config(), state, clock=lambda: 4000), [])


if __name__ == "__main__":
    unittest.main()
