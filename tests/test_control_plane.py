import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from installer import control_plane


class ControlPlaneTests(unittest.TestCase):
    @staticmethod
    def release(root: Path) -> Path:
        release = root / ("a" * 16)
        (release / "ansible" / "roles" / "android_farm" / "tasks").mkdir(parents=True)
        (release / "ansible" / "site.yml").write_text("---\n", encoding="utf-8")
        (release / "ansible" / "roles" / "android_farm" / "tasks" / "main.yml").write_text(
            "---\n", encoding="utf-8")
        (release / "services" / "worker").mkdir(parents=True)
        (release / "services" / "worker" / "requirements.txt").write_text(
            "redis==6.4.0\n", encoding="utf-8")
        (release / "services" / "api").mkdir(parents=True)
        (release / "services" / "api" / "requirements.txt").write_text(
            "gunicorn==26.2.0\n", encoding="utf-8")
        (release / "services" / "api" / "server.py").write_text("# reviewed fixture\n", encoding="utf-8")
        return release

    def test_control_plane_uses_private_files_and_never_puts_secret_in_argv(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            release_root = base / "releases"
            release_root.mkdir()
            release = self.release(release_root)
            runtime = base / "run"
            password_file = base / "etc" / "redis-worker-password"
            observed = {}
            redis_probe_complete = False

            def runner(argv, **kwargs):
                nonlocal redis_probe_complete
                self.assertTrue(kwargs["capture_output"])
                self.assertTrue(kwargs["text"])
                self.assertIn("timeout", kwargs)
                if argv[0] == "curl":
                    self.assertIn("--unix-socket", argv)
                    self.assertNotIn("--user", argv)
                    return subprocess.CompletedProcess(argv, 0, "401", "")
                if argv[0] == "docker":
                    self.assertEqual(argv[1:4], ["exec", "farm-console", "wget"])
                    return subprocess.CompletedProcess(argv, 1, "", "  HTTP/1.1 401 Unauthorized\n")
                if argv[0] == "systemctl":
                    if not redis_probe_complete:
                        redis_probe_complete = True
                        return subprocess.CompletedProcess(argv, 3, "", "")
                    observed.setdefault("systemctl", []).append(list(argv))
                    return subprocess.CompletedProcess(argv, 0, "", "")
                inventory_path = Path(argv[argv.index("--inventory") + 1])
                vars_argument = argv[argv.index("--extra-vars") + 1]
                self.assertTrue(vars_argument.startswith("@"))
                variables_path = Path(vars_argument[1:])
                ansible_config = Path(kwargs["env"]["ANSIBLE_CONFIG"])
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
                variables = json.loads(variables_path.read_text(encoding="utf-8"))
                password = variables["farm_redis_worker_password"]
                self.assertNotIn(password, repr(argv))
                self.assertEqual(variables["farm_devices"], [])
                self.assertTrue(variables["farm_api_enabled"])
                self.assertEqual(variables["farm_release_dir"], str(release.resolve()))
                self.assertIn("localhost", inventory["all"]["children"]["android_farm_hosts"]["hosts"])
                self.assertTrue(ansible_config.name.endswith(".cfg"))
                self.assertIn("[defaults]", ansible_config.read_text(encoding="utf-8"))
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(inventory_path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(variables_path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(ansible_config.stat().st_mode), 0o600)
                observed.update(inventory=inventory_path, variables=variables_path,
                                ansible_config=ansible_config, password=password, argv=list(argv))
                return subprocess.CompletedProcess(argv, 0, "ok", "")

            with patch("installer.control_plane._require_supported_host"), \
                    patch("installer.control_plane.shutil.which",
                          side_effect=lambda name: ("/usr/bin/ansible-playbook"
                                                    if name == "ansible-playbook" else None)):
                result = control_plane.install_control_plane(
                    release,
                    runner=runner,
                    release_root=release_root,
                    password_file=password_file,
                    redis_config=base / "missing-redis.conf",
                    runtime_dir=runtime,
                    verifier=lambda _: (True, "ok"),
                )

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["devices_declared"], 0)
            self.assertTrue(result["services_validated"])
            self.assertEqual([item[1] for item in observed["systemctl"]],
                             ["is-active", "is-enabled"] * 4)
            self.assertIn(["systemctl", "is-active", "--quiet", "android-farm-api.service"],
                          observed["systemctl"])
            self.assertFalse(observed["inventory"].exists())
            self.assertFalse(observed["variables"].exists())
            self.assertFalse(observed["ansible_config"].exists())
            self.assertEqual(password_file.read_text(encoding="ascii").strip(), observed["password"])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(password_file.stat().st_mode), 0o600)

    def test_existing_unmanaged_redis_is_never_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            release_root = base / "releases"
            release_root.mkdir()
            release = self.release(release_root)
            redis_config = base / "redis.conf"
            original = b"bind 127.0.0.1\nappendonly no\n"
            redis_config.write_bytes(original)
            runner = Mock(return_value=subprocess.CompletedProcess([], 3, "", ""))
            with patch("installer.control_plane._require_supported_host"), \
                    patch("installer.control_plane.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "existing unmanaged Redis"):
                    control_plane.install_control_plane(
                        release,
                        runner=runner,
                        release_root=release_root,
                        password_file=base / "missing-marker",
                        redis_config=redis_config,
                        runtime_dir=base / "run",
                        verifier=lambda _: (True, "ok"),
                    )
            self.assertEqual(redis_config.read_bytes(), original)
            self.assertFalse((base / "missing-marker").exists())
            self.assertEqual(runner.call_count, 1)

    def test_password_is_reused_and_invalid_existing_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            path.write_text("A" * 48 + "\n", encoding="ascii")
            path.chmod(0o600)
            self.assertEqual(control_plane._redis_password(path), "A" * 48)
            path.write_text("not valid whitespace\n", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "invalid value"):
                control_plane._redis_password(path)

    def test_ansible_is_installed_from_ubuntu_package_only_when_missing(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("installer.control_plane.shutil.which",
                   side_effect=[None, "/usr/bin/ansible-playbook"]):
            executable = control_plane._ensure_ansible(runner=runner)
        self.assertEqual(executable, "/usr/bin/ansible-playbook")
        self.assertEqual(calls[0], ["apt-get", "update"])
        self.assertEqual(
            calls[1],
            ["apt-get", "install", "-y", "--no-install-recommends", "ansible-core"],
        )

        calls.clear()
        with patch("installer.control_plane.shutil.which",
                   return_value="/usr/bin/ansible-playbook"):
            control_plane._ensure_ansible(runner=runner)
        self.assertEqual(calls, [])

    def test_ansible_failure_never_echoes_captured_secret(self):
        secret = "S" * 48

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2, secret, secret)

        with self.assertRaises(RuntimeError) as raised:
            control_plane._run_quiet(
                runner, ["ansible-playbook"], stage="Ansible role application"
            )
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("Ansible role application", str(raised.exception))

    def test_release_must_be_direct_child_with_immutable_id(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "releases"
            root.mkdir()
            release = self.release(root)
            self.assertEqual(
                control_plane._validate_release(
                    release, release_root=root, verifier=lambda _: (True, "ok")
                ),
                release.resolve(),
            )
            outside = base / ("b" * 16)
            outside.mkdir()
            with self.assertRaisesRegex(RuntimeError, "outside"):
                control_plane._validate_release(
                    outside, release_root=root, verifier=lambda _: (True, "ok")
                )

    def test_api_listener_retries_startup_and_requires_authentication(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 7, "000", "starting"),
                                  subprocess.CompletedProcess([], 0, "401", "")])
        with patch("installer.control_plane.time.sleep"):
            control_plane._wait_api_listener(runner)
        self.assertEqual(runner.call_count, 2)
        for status in ("200", "404", "503"):
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, status, ""))
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "authentication"):
                control_plane._wait_api_listener(runner, timeout=0)

    def test_root_launcher_contract_is_non_destructive_and_forwards_arguments(self):
        script = Path("install.sh").read_text(encoding="utf-8")
        self.assertIn("https://github.com/mjpouladi/AndroidFarm.git", script)
        self.assertIn("status --porcelain=v1 --untracked-files=all", script)
        self.assertIn("merge --ff-only --no-edit", script)
        self.assertIn("ca-certificates", script)
        self.assertIn("-type l -print -quit", script)
        self.assertIn("installer/quickstart.py\" \"$@\"", script)
        self.assertNotIn("git reset", script)
        self.assertNotIn("git clean", script)
        self.assertNotIn("rm -r", script)
        self.assertNotIn("coolify", script.lower())

    def test_console_probe_rejects_spa_success_and_upstream_failure(self):
        for code in ('200', '404', '502', '503'):
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', f'HTTP/1.1 {code} result'))
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, 'console cannot reach'):
                control_plane._wait_console_api(runner, timeout=0)
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'HTTP/1.1 401 Unauthorized'))
        control_plane._wait_console_api(runner, timeout=0)

    @staticmethod
    def managed_console():
        return {"Id": "c" * 64, "Name": "/farm-console", "State": {"Running": True, "Paused": False},
                "Config": {"Labels": {"farm.stack": "core", "farm.role": "console",
                                       "com.docker.compose.project": "android-farm-core",
                                       "com.docker.compose.service": "console"}},
                "Mounts": [{"Type": "bind", "Source": str(control_plane.API_SOCKET.parent),
                            "Destination": "/run/farm-api", "RW": False}]}

    def test_stale_socket_bind_recovers_only_managed_console_and_rechecks_route(self):
        calls = []
        restarted = False

        def runner(argv, **kwargs):
            nonlocal restarted
            calls.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps([self.managed_console()]), "")
            if argv[:2] == ["docker", "restart"]:
                restarted = True
                return subprocess.CompletedProcess(argv, 0, "c" * 64, "")
            if argv[0] == "curl":
                return subprocess.CompletedProcess(argv, 0, "401", "")
            if argv[3] == "test":
                return subprocess.CompletedProcess(argv, 1, "", "")
            if argv[3] == "wget":
                return subprocess.CompletedProcess(argv, 1, "", "HTTP/1.1 " + ("401 Unauthorized" if restarted else "502 Bad Gateway"))
            self.fail("unexpected recovery command")

        with patch.object(control_plane, "_protected_api_socket", return_value=True):
            self.assertTrue(control_plane._recover_console_socket_bind(runner))
        restarts = [command for command in calls if command[:2] == ["docker", "restart"]]
        self.assertEqual(restarts, [["docker", "restart", "--time", "10", "c" * 64]])
        self.assertEqual(calls[-1][1:4], ["exec", "farm-console", "wget"])

    def test_stale_socket_recovery_rejects_wrong_identity_or_mount_before_mutation(self):
        for mismatch in ("project", "role", "name", "source", "writeable", "nested", "id"):
            item = self.managed_console()
            if mismatch == "project":
                item["Config"]["Labels"]["com.docker.compose.project"] = "unrelated"
            elif mismatch == "role":
                item["Config"]["Labels"]["farm.role"] = "android"
            elif mismatch == "name":
                item["Name"] = "/another-console"
            elif mismatch == "source":
                item["Mounts"][0]["Source"] = "/data/coolify/generated-path"
            elif mismatch == "writeable":
                item["Mounts"][0]["RW"] = True
            elif mismatch == "nested":
                item["Mounts"].append({"Destination": "/run/farm-api/control.sock"})
            elif mismatch == "id":
                item["Id"] = "--invalid"
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([item]), ""))
            with self.subTest(mismatch=mismatch), \
                 patch.object(control_plane, "console_api_status", return_value="502"), \
                 patch.object(control_plane, "_listener_status", return_value="401"), \
                 patch.object(control_plane, "_protected_api_socket", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "identity or exact read-only socket bind"):
                    control_plane._recover_console_socket_bind(runner)
                self.assertEqual([call.args[0] for call in runner.call_args_list], [["docker", "inspect", "farm-console"]])

    def test_socket_recovery_never_restarts_healthy_console_or_unreachable_api(self):
        for console, host in (("401", "401"), ("502", "unreachable"), ("200", "401"), ("503", "401")):
            runner = Mock()
            with self.subTest(console=console, host=host), \
                 patch.object(control_plane, "console_api_status", return_value=console), \
                 patch.object(control_plane, "_listener_status", return_value=host):
                self.assertFalse(control_plane._recover_console_socket_bind(runner))
                runner.assert_not_called()
        with patch.object(control_plane, "console_api_status", return_value="502"), \
             patch.object(control_plane, "_listener_status", return_value="401"), \
             patch.object(control_plane, "_protected_api_socket", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "not protected"):
                control_plane._recover_console_socket_bind(runner)
            runner.assert_not_called()

    def test_socket_recovery_refuses_parent_tmpfs_that_restart_cannot_fix(self):
        for path in ("/run", "/var/run", "/run/farm-api", "/var/run/farm-api/control.sock"):
            item = self.managed_console()
            item["HostConfig"] = {"Tmpfs": {path: ""}}
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([item]), ""))
            with self.subTest(path=path), \
                 patch.object(control_plane, "console_api_status", return_value="502"), \
                 patch.object(control_plane, "_listener_status", return_value="401"), \
                 patch.object(control_plane, "_protected_api_socket", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "tmpfs masks the API socket bind"):
                    control_plane._recover_console_socket_bind(runner)
                self.assertEqual([call.args[0] for call in runner.call_args_list], [["docker", "inspect", "farm-console"]])

    def test_socket_recovery_requires_actual_absence_and_does_not_loop_restart(self):
        for present_flag in ("-S", "-e", "-L"):
            def runner(argv, **kwargs):
                if argv[1] == "inspect":
                    return subprocess.CompletedProcess(argv, 0, json.dumps([self.managed_console()]), "")
                self.assertEqual(argv[1], "exec")
                return subprocess.CompletedProcess(argv, 0 if argv[4] == present_flag else 1, "", "")
            with self.subTest(present_flag=present_flag), \
                 patch.object(control_plane, "console_api_status", return_value="502"), \
                 patch.object(control_plane, "_listener_status", return_value="401"), \
                 patch.object(control_plane, "_protected_api_socket", return_value=True):
                self.assertFalse(control_plane._recover_console_socket_bind(runner))
        runner = Mock(side_effect=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0 if argv[1] != "exec" else 1,
            json.dumps([self.managed_console()]) if argv[1] == "inspect" else "", ""))
        with patch.object(control_plane, "console_api_status", return_value="502"), \
             patch.object(control_plane, "_listener_status", return_value="401"), \
             patch.object(control_plane, "_protected_api_socket", return_value=True), \
             patch.object(control_plane, "_wait_console_api", side_effect=RuntimeError("route remains 502")):
            with self.assertRaisesRegex(RuntimeError, "route remains 502"):
                control_plane._recover_console_socket_bind(runner)
        self.assertEqual(sum(call.args[0][1] == "restart" for call in runner.call_args_list), 1)

    def test_diagnostics_are_read_only_and_do_not_echo_journal_or_environment(self):
        secret = 'private-diagnostic-fixture-never-print'
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'systemctl':
                return subprocess.CompletedProcess(argv, 0, '\n'.join([
                    'LoadState=loaded', 'ActiveState=active', 'SubState=running',
                    'UnitFileState=enabled', 'Result=success', 'ExecMainStatus=0', 'NRestarts=0',
                    'Environment=' + secret]), '')
            if argv[0] == 'curl':
                return subprocess.CompletedProcess(argv, 0, '401', '')
            if argv[0] == 'docker':
                return subprocess.CompletedProcess(argv, 1, '', 'HTTP/1.1 401 Unauthorized')
            if argv[0] == 'journalctl':
                return subprocess.CompletedProcess(argv, 0, f'PermissionError: {secret}\n', '')
            self.fail('unexpected mutating command')

        socket_path = Mock()
        socket_path.lstat.return_value = Mock(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_gid=101)
        result = control_plane.diagnose_control_plane(runner=runner, socket_path=socket_path, include_journal=True)
        self.assertTrue(result['ready'])
        self.assertEqual(result['journal_categories'], ['permission-denied'])
        self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(all(command[1] == 'show' for command in calls if command[0] == 'systemctl'))
        socket_path.lstat.return_value.st_mode = stat.S_IFREG | 0o660
        self.assertFalse(control_plane.diagnose_control_plane(runner=runner, socket_path=socket_path)['ready'])

    def test_failure_categories_are_fixed_and_never_contain_command_output(self):
        self.assertEqual(control_plane.failure_categories('password=secret-token\nModuleNotFoundError: gunicorn'),
                         ['dependency-missing'])
        self.assertEqual(control_plane.failure_categories(
            "[ERROR] Control server error: [Errno 30] Read-only file system: '/root/.gunicorn'"),
            ['filesystem-read-only', 'gunicorn-control-path'])
        self.assertEqual(control_plane.failure_categories('another web executor is still active'),
                         ['queue-already-owned'])


if __name__ == "__main__":
    unittest.main()
