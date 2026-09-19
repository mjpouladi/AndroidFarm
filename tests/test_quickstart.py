import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from installer import install, quickstart


class QuickstartTests(unittest.TestCase):
    def test_state_does_not_persist_tokens_or_passwords(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = quickstart.load_state(path)
            state.update(coolify_url="https://coolify.example.com", token="secret-token",
                         password="private-password", phase="host_prepared")
            quickstart.save_state(state, path)
            text = path.read_text()
            self.assertNotIn("secret-token", text)
            self.assertNotIn("private-password", text)
            self.assertEqual(quickstart.load_state(path)["installation_id"], state["installation_id"])

    def test_dirty_checkout_and_foreign_repository_are_rejected(self):
        runner = Mock(side_effect=[Mock(stdout=quickstart.REPOSITORY), Mock(stdout=" M ops/farmctl.py")])
        with self.assertRaisesRegex(RuntimeError, "تغییر محلی"):
            quickstart.source_commit(Path("repo"), runner)
        runner = Mock(return_value=Mock(stdout="https://example.com/other.git"))
        with self.assertRaisesRegex(RuntimeError, "مخزن رسمی"):
            quickstart.source_commit(Path("repo"), runner)

    def test_deploy_wait_is_bounded_and_does_not_print_remote_body(self):
        client = Mock()
        client.deployment_status.side_effect = [{"status": "queued"}, {"status": "finished"}]
        report = Mock()
        quickstart.wait_deployment(client, "deploy", clock=Mock(side_effect=[0, 1, 2]),
                                  sleep=Mock(), report=report)
        self.assertEqual(client.deployment_status.call_count, 2)
        client.deployment_status.side_effect = None
        client.deployment_status.return_value = {"status": "failed", "logs": "token=secret"}
        with self.assertRaisesRegex(RuntimeError, "ناموفق") as failure:
            quickstart.wait_deployment(client, "deploy", clock=lambda: 0, sleep=Mock())
        self.assertNotIn("secret", str(failure.exception))
        with self.assertRaisesRegex(RuntimeError, "مهلت"):
            quickstart.wait_deployment(client, "deploy", timeout=10, clock=Mock(side_effect=[0, 11]))

    def test_resume_reuses_deployment_only_for_same_commit_and_environment(self):
        state = {"deployment_uuid": "deploy", "deployment_commit": "a" * 40,
                 "deployment_env_hash": "old-env"}
        self.assertFalse(quickstart.deployment_needed(state, "a" * 40, "old-env"))
        self.assertTrue(quickstart.deployment_needed(state, "b" * 40, "old-env"))
        self.assertTrue(quickstart.deployment_needed(state, "a" * 40, "new-env"))

    def test_uncertain_deploy_response_is_recovered_without_another_post(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = quickstart.load_state(path)
            state.update(app_uuid="app", deployment_requested=True,
                         deployment_before=["old"], deployment_commit="a" * 40)
            client = Mock()
            client.list_application_deployments.return_value = [
                {"deployment_uuid": "new", "commit": "a" * 40, "status": "finished"},
                {"deployment_uuid": "old", "status": "finished"}]
            quickstart.resume_deployments(client, state, path)
            self.assertEqual(state["deployment_uuid"], "new")
            self.assertFalse(state["deployment_requested"])
            client.deploy.assert_not_called()

    def test_unknown_deploy_without_history_requires_explicit_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = quickstart.load_state(path)
            state.update(app_uuid="app", deployment_requested=True, deployment_before=[])
            client = Mock()
            client.list_application_deployments.return_value = []
            with self.assertRaisesRegex(RuntimeError, "retry-deploy"):
                quickstart.resume_deployments(client, state, path)
            self.assertTrue(state["deployment_requested"])
            quickstart.resume_deployments(client, state, path, retry_unknown=True)
            self.assertFalse(state["deployment_requested"])
            client.deploy.assert_not_called()

    def test_auth_generation_reuses_password_and_preserves_manual_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = install.Paths(config_dir=root / "config", traefik_dynamic_dir=root / "dynamic")
            state = {}
            user, path = quickstart.prepare_auth(paths, state)
            original = path.read_bytes()
            self.assertEqual(user, "operator")
            self.assertEqual(quickstart.prepare_auth(paths, state)[1].read_bytes(), original)
            path.unlink()
            paths.traefik_dynamic_dir.mkdir()
            manual = paths.traefik_dynamic_dir / "farm-users.htpasswd"
            manual.write_text("old-account:$2y$not-a-real-hash\n")
            manual.chmod(0o600)
            self.assertEqual(quickstart.prepare_auth(paths, {}), (None, None))
            self.assertEqual(manual.read_text(), "old-account:$2y$not-a-real-hash\n")

    def test_core_health_rejects_stopped_or_unhealthy_containers(self):
        containers = [{"Name": "/" + name, "State": {"Running": True}}
                      for name in quickstart.CORE_CONTAINERS]
        containers[1]["State"]["Running"] = False
        containers[2]["State"]["Health"] = {"Status": "unhealthy"}
        result = quickstart.core_runtime_status(runner=Mock(return_value=Mock(
            returncode=0, stdout=json.dumps(containers))))
        self.assertTrue(result["farm-anchor"])
        self.assertFalse(result["farm-console"])
        self.assertFalse(result["android-farm-prometheus"])

    def test_host_handoff_coolify_and_control_plane_resume_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_path = root / "quickstart.json"
            settings = install.Settings(root, "farm.example.com", "console.farm.example.com",
                                        "auto", "auto", install.Paths())
            state = quickstart.load_state(state_path)
            env = {"FARM_RELEASE_ID": "a" * 16, "GRAFANA_DOMAIN": "metrics.farm.example.com"}
            result = {"release_id": "a" * 16, "release_dir": "/opt/android-farm/releases/" + "a" * 16,
                      "coolify_env_file": "/etc/android-farm/coolify.env", "configured": True}
            client = Mock()
            client.ensure_core.return_value = Mock(app_uuid="app")
            client.list_application_deployments.return_value = []
            client.deploy.return_value = "deploy"
            client.deployment_status.return_value = {"status": "finished"}
            control = Mock()
            with patch.object(install, "active_device_count", return_value=0), \
                 patch.object(install, "anchor_labels", return_value={}), \
                 patch.object(install, "apply", return_value={"result": result,
                                                           "detected": {"coolify_network": "coolify"}}), \
                 patch.object(install, "_existing_env", return_value=env), \
                 patch.object(install, "doctor", return_value={"status": "ready", "checks": []}), \
                 patch.object(quickstart, "wait_anchor"), \
                 patch.object(quickstart, "core_runtime_status", return_value={"core": True}) as core_status, \
                 patch.object(quickstart, "https_auth_ready", return_value=True), \
                 patch.object(quickstart, "say"):
                first = quickstart.run_setup(settings, client, state, state_path=state_path,
                                             commit="a" * 40, server_uuid="server", control_installer=control)
                self.assertTrue(first["ready"])
                self.assertEqual(quickstart.load_state(state_path)["phase"], "ready")
                second = quickstart.run_setup(settings, client, state, state_path=state_path,
                                              commit="a" * 40, server_uuid="server", control_installer=control)
                self.assertTrue(second["ready"])
                client.deploy.assert_called_once()
                self.assertEqual(control.call_count, 2)  # idempotent role reconciles services
                # Pruned deployment history is repaired without an endless 404.
                client.deployment_status.side_effect = [
                    quickstart.CoolifyError("history pruned", status=404), {"status": "finished"}]
                repaired = quickstart.run_setup(settings, client, state, state_path=state_path,
                                                commit="a" * 40, server_uuid="server", control_installer=control)
                self.assertTrue(repaired["ready"])
                self.assertEqual(client.deploy.call_count, 2)
                # A previously finished deploy does not prove containers are still running.
                client.deployment_status.side_effect = None
                core_status.side_effect = [{"core": False}, {"core": True}]
                restored = quickstart.run_setup(settings, client, state, state_path=state_path,
                                                commit="a" * 40, server_uuid="server", control_installer=control)
                self.assertTrue(restored["ready"])
                self.assertEqual(client.deploy.call_count, 3)

    def test_failed_deploy_never_finalizes_host_or_installs_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = install.Settings(root, "farm.example.com", "console.farm.example.com",
                                        "auto", "auto", install.Paths())
            state = quickstart.load_state(root / "state.json")
            control = Mock()
            client = Mock()
            client.ensure_core.return_value = Mock(app_uuid="app")
            client.list_application_deployments.return_value = []
            client.deploy.return_value = "deploy"
            client.deployment_status.return_value = {"status": "failed"}
            with patch.object(install, "active_device_count", return_value=0), \
                 patch.object(install, "anchor_labels", return_value={}), \
                 patch.object(install, "apply", return_value={"result": {"release_id": "a" * 16,
                                    "coolify_env_file": "/env"}, "detected": {"coolify_network": "coolify"}}) as apply, \
                 patch.object(install, "_existing_env", return_value={"FARM_RELEASE_ID": "a" * 16}), \
                 patch.object(quickstart, "say"):
                with self.assertRaisesRegex(RuntimeError, "ناموفق"):
                    quickstart.run_setup(settings, client, state, state_path=root / "state.json",
                                         commit="a" * 40, server_uuid="server", control_installer=control)
                apply.assert_called_once()
                control.assert_not_called()
                self.assertEqual(quickstart.load_state(root / "state.json")["deployment_uuid"], "deploy")


if __name__ == "__main__":
    unittest.main()
