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


if __name__ == "__main__":
    unittest.main()
