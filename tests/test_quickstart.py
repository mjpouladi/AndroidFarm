from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from installer import install, quickstart


class QuickstartTests(unittest.TestCase):
    def test_new_install_defaults_to_https_and_resume_never_reprompts(self):
        args = quickstart.build_parser().parse_args([])
        reader = Mock(return_value="farm.example.com")
        selected = quickstart.select_access(args, {}, {}, reader=reader)
        self.assertEqual(selected, {"access_mode": "domain", "public_ip": None,
                                   "http_port": 18080, "farm_domain": "farm.example.com",
                                   "console_domain": "farm.example.com"})
        reader.assert_called_once()
        reader.reset_mock()
        self.assertEqual(quickstart.select_access(args, selected, {}, reader=reader), selected)
        reader.assert_not_called()
        ip_args = quickstart.build_parser().parse_args(["--ip", "192.168.50.20"])
        ip = quickstart.select_access(ip_args, {}, {}, reader=reader)
        self.assertEqual(ip["access_mode"], "ip")
        self.assertEqual(quickstart.select_access(args, ip, {}, reader=reader), ip)
        reader.assert_not_called()

    def test_domain_install_is_preserved_and_explicit_flags_switch_modes(self):
        old = {"farm_domain": "farm.example.com", "console_domain": "panel.example.com"}
        args = quickstart.build_parser().parse_args([])
        selected = quickstart.select_access(args, {}, old)
        self.assertEqual(selected["access_mode"], "domain")
        self.assertEqual(selected["console_domain"], "panel.example.com")
        args = quickstart.build_parser().parse_args(["--ip", "192.168.50.20", "--port", "18090"])
        ip = quickstart.select_access(args, old, {})
        self.assertEqual(ip["http_port"], 18090)
        args = quickstart.build_parser().parse_args(["--domain", "new.example.com"])
        domain = quickstart.select_access(args, ip, {})
        self.assertIsNone(domain["public_ip"])
        self.assertEqual(domain["console_domain"], "new.example.com")

    def test_adb_port_is_rejected_and_domain_gateway_port_can_be_changed(self):
        args = quickstart.build_parser().parse_args(["--ip", "192.168.50.20", "--port", "5551"])
        with self.assertRaises(ValueError):
            quickstart.select_access(args, {}, {})
        args = quickstart.build_parser().parse_args(["--domain", "farm.example.com", "--port", "18090"])
        selected = quickstart.select_access(args, {}, {})
        self.assertEqual(selected["access_mode"], "domain")
        self.assertEqual(selected["http_port"], 18090)

    def test_console_can_share_apex_with_device_routes(self):
        args = quickstart.build_parser().parse_args([
            "--domain", "commex-box.com", "--console-domain", "commex-box.com"])
        selected = quickstart.select_access(args, {}, {})
        self.assertEqual(selected["farm_domain"], "commex-box.com")
        self.assertEqual(selected["console_domain"], "commex-box.com")
        saved = quickstart.select_access(quickstart.build_parser().parse_args([]), selected, {})
        self.assertEqual(saved, selected)
        bad = quickstart.build_parser().parse_args([
            "--ip", "192.168.50.20", "--console-domain", "commex-box.com"])
        with self.assertRaises(ValueError):
            quickstart.select_access(bad, {}, {})

    def test_ip_urls_share_origin_and_domain_urls_preserve_https(self):
        settings = install.Settings(Path("repo"), "192.168.50.20", "192.168.50.20",
                                    "auto", "auto", install.Paths(), access_mode="ip",
                                    public_ip="192.168.50.20", http_port=18090)
        self.assertEqual(quickstart.access_urls(settings), {
            "console": "http://192.168.50.20:18090/",
            "monitoring": "http://192.168.50.20:18090/metrics/"})
        settings = install.Settings(Path("repo"), "farm.example.com", "panel.example.com",
                                    "auto", "auto", install.Paths())
        self.assertEqual(quickstart.access_urls(settings), {
            "console": "https://panel.example.com/", "monitoring": "https://metrics.farm.example.com/"})
        self.assertEqual(quickstart.access_urls(settings, {"GRAFANA_DOMAIN": "monitor.example.com"})[
            "monitoring"], "https://monitor.example.com/")

    def test_auth_smoke_requires_basic_challenge_and_does_not_follow_redirects(self):
        url = "http://192.168.50.20:18080/metrics/"
        opener = Mock()
        with patch.object(quickstart.urllib.request, "build_opener", return_value=opener):
            opener.open.side_effect = urllib.error.HTTPError(url, 401, "auth",
                                                            {"WWW-Authenticate": 'Basic realm="farm"'}, None)
            self.assertTrue(quickstart.web_auth_ready(url))
            opener.open.assert_called_once_with(url, timeout=10)
            opener.open.side_effect = urllib.error.HTTPError(url, 401, "auth", {}, None)
            self.assertFalse(quickstart.web_auth_ready(url))
            opener.open.side_effect = urllib.error.HTTPError(url, 308, "redirect", {}, None)
            self.assertFalse(quickstart.web_auth_ready(url))
            opener.open.reset_mock()
            self.assertFalse(quickstart.web_auth_ready("http://public.example.com:18080/"))
            self.assertFalse(quickstart.web_auth_ready("http://192.168.50.20:5551/"))
            self.assertFalse(quickstart.web_auth_ready("http://127.0.0.1:18080/"))
            opener.open.assert_not_called()
            opener.open.side_effect = urllib.error.HTTPError(url, 401, "auth",
                                                            {"WWW-Authenticate": 'Basic realm="farm"'}, None)
            self.assertTrue(quickstart.web_auth_ready("http://127.0.0.1:18080/", allow_loopback=True))
        self.assertIsNone(quickstart.NoRedirect().redirect_request(None, None, 301, "", {}, url))

    def test_state_does_not_persist_tokens_or_passwords(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = quickstart.load_state(path)
            state.update(coolify_url="https://coolify.example.com", token="secret-token",
                         password="private-password", phase="host_prepared", access_mode="ip",
                         public_ip="192.168.50.20", http_port=18080)
            quickstart.save_state(state, path)
            text = path.read_text()
            self.assertNotIn("secret-token", text)
            self.assertNotIn("private-password", text)
            self.assertEqual(quickstart.load_state(path)["installation_id"], state["installation_id"])
            self.assertEqual(quickstart.load_state(path)["access_mode"], "ip")
            self.assertEqual(quickstart.load_state(path)["public_ip"], "192.168.50.20")
            self.assertEqual(quickstart.load_state(path)["http_port"], 18080)

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
            self.assertEqual(user, "mjpouladi")
            self.assertEqual(quickstart.prepare_auth(paths, state)[1].read_bytes(), original)
            path.unlink()
            paths.traefik_dynamic_dir.mkdir()
            manual = paths.traefik_dynamic_dir / "farm-users.htpasswd"
            manual.write_text("old-account:$2y$not-a-real-hash\n")
            manual.chmod(0o600)
            self.assertEqual(quickstart.prepare_auth(paths, {}), (None, None))
            self.assertEqual(manual.read_text(), "old-account:$2y$not-a-real-hash\n")

    def test_explicit_password_rotation_preserves_managed_username_and_private_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = install.Paths(config_dir=root / "config", traefik_dynamic_dir=root / "dynamic")
            state = {"auth_user": "qa.operator"}
            user, password = quickstart.prepare_auth(paths, state)
            original = password.read_bytes()
            paths.traefik_dynamic_dir.mkdir()
            users = paths.traefik_dynamic_dir / "farm-users.htpasswd"
            users.write_text("qa.operator:$2y$12$" + "a" * 53 + "\n")
            users.chmod(0o600)
            original_hash = users.read_bytes()
            with patch.object(install, "_atomic_write", wraps=install._atomic_write) as write:
                rotated_user, rotated_path = quickstart.prepare_auth(paths, state, rotate=True)
                self.assertEqual(write.call_args.args[2], 0o600)
            self.assertEqual((user, rotated_user, state["auth_user"]), ("qa.operator",) * 3)
            self.assertEqual(rotated_path, password)
            self.assertNotEqual(password.read_bytes(), original)
            self.assertGreaterEqual(len(install._private_password(password)), 40)
            if os.name == "posix":
                self.assertEqual(password.stat().st_uid, 0)
                self.assertEqual(password.stat().st_mode & 0o777, 0o600)
            # Applying bcrypt to both web routes remains the host installer's job.
            self.assertEqual(users.read_bytes(), original_hash)
            rotated = password.read_bytes()
            self.assertEqual(quickstart.prepare_auth(paths, state), (user, password))
            self.assertEqual(password.read_bytes(), rotated)

    def test_rotation_adopts_only_a_single_safe_manual_username(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = install.Paths(config_dir=root / "config", traefik_dynamic_dir=root / "dynamic")
            paths.traefik_dynamic_dir.mkdir()
            users = paths.traefik_dynamic_dir / "farm-users.htpasswd"
            for invalid in ("one:$2b$hash\ntwo:$2b$hash\n", "bad user:$2b$hash\n",
                            "operator:plaintext\n", "\n", "x" * 4097):
                with self.subTest(record=invalid[:30]):
                    users.write_text(invalid)
                    users.chmod(0o600)
                    with self.assertRaises(RuntimeError):
                        quickstart.prepare_auth(paths, {}, rotate=True)
                    self.assertFalse((paths.config_dir / "web-login-password").exists())
                    self.assertEqual(users.read_text(), invalid)
            users.write_text("manual.owner:$2b$12$" + "b" * 53 + "\n")
            users.chmod(0o600)
            state = {}
            user, password = quickstart.prepare_auth(paths, state, rotate=True)
            self.assertEqual(user, "manual.owner")
            self.assertEqual(state["auth_user"], user)
            self.assertEqual(len(install._private_password(password)), 40)

    def test_rotation_refuses_unsafe_existing_files_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = install.Paths(config_dir=root / "config", traefik_dynamic_dir=root / "dynamic")
            state = {"auth_user": "operator"}
            _, password = quickstart.prepare_auth(paths, state)
            original = password.read_bytes()
            with patch.object(install, "_private_password", side_effect=RuntimeError("unsafe password")), \
                 patch.object(install, "_atomic_write") as write:
                with self.assertRaisesRegex(RuntimeError, "unsafe password"):
                    quickstart.prepare_auth(paths, state, rotate=True)
                write.assert_not_called()
            paths.traefik_dynamic_dir.mkdir()
            users = paths.traefik_dynamic_dir / "farm-users.htpasswd"
            users.write_text("operator:$2b$hash\n")
            users.chmod(0o600)
            with patch.object(install, "traefik_users_status", return_value=(False, "unsafe owner")), \
                 patch.object(install, "_atomic_write") as write:
                with self.assertRaises(RuntimeError):
                    quickstart.prepare_auth(paths, state, rotate=True)
                write.assert_not_called()
            users.write_text("different-user:$2b$hash\n")
            with self.assertRaisesRegex(RuntimeError, "نام کاربری"):
                quickstart.prepare_auth(paths, state, rotate=True)
            self.assertEqual(password.read_bytes(), original)
            self.assertEqual(state["auth_user"], "operator")

    def test_main_never_prints_password_even_to_tty_and_wires_explicit_rotation(self):
        class Output(io.StringIO):
            def __init__(self, tty):
                super().__init__()
                self.tty = tty

            def isatty(self):
                return self.tty

        private_path = Path("/etc/android-farm/web-login-password")
        outcome = {"ready": True, "doctor": {"checks": []}, "web": {}, "core": {},
                   "urls": {"console": "https://farm.example.com/",
                            "monitoring": "https://metrics.farm.example.com/"}, "app_uuid": "app"}
        for tty in (False, True):
            for rotate in (False, True):
                with self.subTest(tty=tty, rotate=rotate):
                    output = Output(tty)
                    with patch.object(quickstart.sys, "stdout", output), \
                         patch.object(quickstart, "require_host"), \
                         patch.object(quickstart, "setup_lock", return_value=nullcontext()), \
                         patch.object(quickstart, "load_state", return_value={}), \
                         patch.object(quickstart, "save_state"), \
                         patch.object(quickstart, "source_commit", return_value="a" * 40), \
                         patch.object(quickstart, "select_server", return_value="server"), \
                         patch.object(quickstart, "prepare_auth", return_value=("operator", private_path)) as auth, \
                         patch.object(quickstart, "run_setup", return_value=outcome), \
                         patch.object(quickstart.getpass, "getpass", return_value="test-api-token"), \
                         patch("installer.coolify_api.CoolifyClient"), \
                         patch.object(install, "_read_json_if_regular", return_value={}), \
                         patch.object(install, "_private_password", return_value="never-print-this") as read:
                        arguments = ["--domain", "farm.example.com", "--coolify-url", "http://127.0.0.1:8000"]
                        if rotate:
                            arguments.append("--rotate-web-password")
                        self.assertEqual(quickstart.main(arguments), 0)
                        self.assertEqual(auth.call_args.kwargs, {"rotate": rotate})
                        read.assert_not_called()
                    self.assertNotIn("never-print-this", output.getvalue())
                    self.assertNotIn("test-api-token", output.getvalue())
                    self.assertIn(str(private_path), output.getvalue())
                    self.assertIn("operator", output.getvalue())

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

    def test_admin_password_validation_is_bounded_and_never_echoes_input(self):
        for value in ('too-short', 'x' * 73, 'p' * 12 + '\n', ' p' * 12, 'رمز' * 13):
            with self.subTest(length=len(value)), self.assertRaises(ValueError) as raised:
                quickstart.validate_admin_password(value)
            self.assertNotIn(value, str(raised.exception))
        quickstart.validate_admin_password('safe-fixture-password-123')
        with patch.object(quickstart.getpass, 'getpass', side_effect=['safe-fixture-password-123', 'different-fixture']):
            with self.assertRaisesRegex(ValueError, 'یکسان'):
                quickstart.ask_admin_password()

    def test_admin_password_prompt_rejects_echoing_fallback(self):
        def unavailable_terminal(*args):
            quickstart.warnings.warn('Password input may be echoed.', quickstart.getpass.GetPassWarning)
            self.fail('echoed password input must not run')
        with patch.object(quickstart.getpass, 'getpass', side_effect=unavailable_terminal):
            with self.assertRaisesRegex(RuntimeError, 'ترمینال تعاملی SSH'):
                quickstart.ask_admin_password()

    def test_explicit_admin_setup_preserves_old_defaults_and_resumes_pending_rename(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = install.Paths(config_dir=root / 'config', traefik_dynamic_dir=root / 'dynamic')
            paths.config_dir.mkdir(mode=0o700)
            with self.assertRaises(RuntimeError):
                quickstart.prepare_auth(paths, {}, username='-invalid', password='safe-fixture-password')
            self.assertFalse((paths.config_dir / 'web-login-password').exists())
            password_path = paths.config_dir / 'web-login-password'
            password_path.write_text('old-safe-fixture-password\n')
            password_path.chmod(0o600)
            user, _ = quickstart.prepare_auth(paths, {})
            self.assertEqual(user, 'operator')  # existing installs never silently rename
            paths.traefik_dynamic_dir.mkdir()
            auth = paths.traefik_dynamic_dir / 'farm-users.htpasswd'
            auth.write_text('operator:$2y$fixture\n')
            auth.chmod(0o600)
            state = {'auth_user': 'operator'}
            user, _ = quickstart.prepare_auth(paths, state, username='qa.owner', password='new-safe-fixture-password')
            self.assertEqual(user, 'qa.owner')
            self.assertEqual(password_path.read_text(), 'new-safe-fixture-password\n')
            self.assertEqual(auth.read_text(), 'operator:$2y$fixture\n')  # host apply changes the hash
            state['credentials_sync_pending'] = True
            self.assertEqual(quickstart.prepare_auth(paths, state, username='qa.owner')[0], 'qa.owner')

    def test_diagnose_and_repair_do_not_request_coolify_credentials(self):
        for argument, function in (('--diagnose', 'diagnose'), ('--repair-control-plane', 'repair_control_plane')):
            with self.subTest(argument=argument), patch.object(quickstart, 'require_host'), \
                 patch.object(quickstart, 'setup_lock', return_value=nullcontext()), \
                 patch.object(quickstart, function, return_value={'ready': True}), \
                 patch.object(quickstart, 'say'), patch.object(quickstart.getpass, 'getpass') as prompt:
                self.assertEqual(quickstart.main([argument]), 0)
                prompt.assert_not_called()

    def test_repair_requires_finalized_release_parity_without_changing_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            state_file = Path(folder) / 'install-state.json'
            state_file.write_text(json.dumps({'status': 'ready', 'release_id': 'a' * 16,
                                             'release_dir': '/opt/android-farm/releases/' + 'a' * 16}))
            state_file.chmod(0o600)
            control = Mock()
            with patch.object(install, 'anchor_labels', return_value={'farm.release': 'b' * 16}):
                with self.assertRaisesRegex(RuntimeError, 'همسان'):
                    quickstart.repair_control_plane(state_path=state_file, control_installer=control)
                control.assert_not_called()
            with patch.object(install, 'anchor_labels', return_value={'farm.release': 'a' * 16}), \
                 patch.object(quickstart, 'diagnose', return_value={'ready': True}):
                self.assertTrue(quickstart.repair_control_plane(state_path=state_file, control_installer=control)['ready'])
                control.assert_called_once_with(Path('/opt/android-farm/releases/' + 'a' * 16))

    def test_authenticated_api_probe_rejects_outer_auth_only_and_never_follows_redirect(self):
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ok"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(install, '_private_password', return_value='fixture-private-password'), \
             patch.object(quickstart.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertTrue(quickstart.authenticated_api_ready('https://farm.example.com/', 'operator', Path('/private')))
            self.assertIn(quickstart.NoRedirect, build.call_args.args)
            self.assertEqual(opener.open.call_args.args[0].full_url, 'https://farm.example.com/api/v1/health')
            response.read.return_value = b'<html>SPA</html>'
            self.assertFalse(quickstart.authenticated_api_ready('https://farm.example.com/', 'operator', Path('/private')))
            self.assertFalse(quickstart.authenticated_api_ready('http://203.0.113.8:18080', 'operator', Path('/private')))

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
                 patch.object(quickstart, "web_auth_ready", return_value=True), \
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
