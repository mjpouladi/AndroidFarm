import json
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from installer import install


class InstallerPureTests(unittest.TestCase):
    def test_docker_available_requires_cli_and_reachable_daemon(self):
        with patch('installer.install.shutil.which', return_value='/usr/bin/docker'), \
                patch('installer.install._command', return_value=Mock(returncode=0)):
            self.assertTrue(install.docker_available())
        with patch('installer.install.shutil.which', return_value='/usr/bin/docker'), \
                patch('installer.install._command', return_value=Mock(returncode=1)):
            self.assertFalse(install.docker_available())
        with patch('installer.install.shutil.which', return_value=None):
            self.assertFalse(install.docker_available())

    def test_bootstrap_requirement_checks_compose_version(self):
        with patch('installer.install.shutil.which', return_value='/usr/bin/tool'), \
                patch('installer.install._command',
                      return_value=Mock(returncode=0, stdout='2.33.1\n')):
            self.assertFalse(install._bootstrap_required())
        with patch('installer.install.shutil.which', return_value='/usr/bin/tool'), \
                patch('installer.install._command',
                      return_value=Mock(returncode=0, stdout='2.20.0\n')):
            self.assertTrue(install._bootstrap_required())

    def test_os_release_and_version_parsing(self):
        parsed = install.parse_os_release('ID="ubuntu"\nVERSION_ID="24.04"\n# ignored\n')
        self.assertEqual(parsed, {"ID": "ubuntu", "VERSION_ID": "24.04"})
        self.assertEqual(install.parse_version("v2.33.1-desktop.1"), (2, 33, 1))
        with self.assertRaises(ValueError):
            install.parse_version("unknown")

    def test_binderfs_readiness_uses_kernel_registration_without_host_device_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            filesystems = root / "filesystems"
            self.assertFalse(install.binderfs_available(filesystems))
            filesystems.write_text("nodev\tsysfs\nnodev\ttmpfs\n\text4\n", encoding="utf-8")
            self.assertFalse(install.binderfs_available(filesystems))
            filesystems.write_text("nodev\tsysfs\nnodev\tbinder\n\text4\n", encoding="utf-8")
            self.assertTrue(install.binderfs_available(filesystems))
            self.assertFalse((root / "binder").exists())
            # Exact filesystem token: a similarly named filesystem is not BinderFS.
            filesystems.write_text("nodev\tbinderfs\nnodev\tbinder-backup\n", encoding="utf-8")
            self.assertFalse(install.binderfs_available(filesystems))

    def test_domain_validation_and_idna(self):
        self.assertEqual(install.normalize_domain("Farm.Example.com."), "farm.example.com")
        self.assertEqual(install.normalize_domain("مثال.com"), "xn--mgbh0fb.com")
        for invalid in ("https://farm.example.com", "farm.example.com/path", "*.example.com", "localhost"):
            with self.assertRaises(ValueError):
                install.normalize_domain(invalid)

    def test_ip_access_validation_and_parser_skip_dns_normalization(self):
        self.assertEqual(install.normalize_public_ip("10.20.30.40"), "10.20.30.40")
        self.assertEqual(install.normalize_public_ip("8.8.8.8"), "8.8.8.8")
        for invalid in ("0.0.0.0", "127.0.0.1", "169.254.1.2", "224.0.0.1", "::1"):
            with self.assertRaises(ValueError):
                install.normalize_public_ip(invalid)
        self.assertEqual(install.normalize_http_port(18080), 18080)
        for invalid in (80, 5551, 8000, 13742, 70000, True):
            with self.assertRaises(ValueError):
                install.normalize_http_port(invalid)

        args = install.build_parser().parse_args([
            "plan", "--ip", "10.20.30.40", "--port", "18080",
            "--farm-domain", "not a domain", "--console-domain", "also invalid",
        ])
        settings = install._settings_from_args(args)
        self.assertEqual(settings.access_mode, "ip")
        self.assertEqual(settings.public_ip, "10.20.30.40")
        self.assertEqual(settings.farm_domain, "10.20.30.40")
        self.assertEqual(settings.console_domain, "10.20.30.40")
        self.assertEqual(settings.http_port, 18080)
        domain_with_port = install.build_parser().parse_args([
            "plan", "--farm-domain", "farm.example.com", "--port", "18090",
        ])
        self.assertEqual(install._settings_from_args(domain_with_port).http_port, 18090)

    def test_network_detection_is_deterministic_and_fails_ambiguous(self):
        self.assertEqual(install.select_coolify_network(["bridge", "coolify"]), "coolify")
        self.assertEqual(install.select_coolify_network(
            ["bridge", "platform"], ["platform"]), "platform")
        self.assertIsNone(install.select_coolify_network(
            ["coolify-a", "coolify-b"], ["coolify-a", "coolify-b"]))

    def test_reserved_network_overlap_is_fail_closed_but_managed_slots_are_allowed(self):
        foreign = [{
            "Name": "legacy-app",
            "Driver": "bridge",
            "Labels": {},
            "IPAM": {"Config": [{"Subnet": "10.231.9.0/24"}]},
        }]
        self.assertEqual(install.docker_network_pool_conflicts(foreign)[0]["network"],
                         "legacy-app")
        lookalike = [{
            "Name": "farm-egress-num01",
            "Driver": "bridge",
            "Labels": {"com.docker.compose.project": "someone-else"},
            "IPAM": {"Config": [{"Subnet": "10.231.0.0/29"}]},
        }]
        self.assertTrue(install.docker_network_pool_conflicts(lookalike))
        managed = [{
            "Name": "farm-egress-num01",
            "Driver": "bridge",
            "Internal": False,
            "Options": {"com.docker.network.bridge.name": "br-af00001"},
            "Labels": {"com.docker.compose.project": install.RUNTIME_COMPOSE_PROJECT},
            "IPAM": {"Config": [{"Subnet": "10.231.0.0/29"}]},
        }, {
            "Name": "farm-control-num01",
            "Driver": "bridge",
            "Internal": True,
            "Labels": {"com.docker.compose.project": install.RUNTIME_COMPOSE_PROJECT},
            "IPAM": {"Config": [{"Subnet": "10.232.0.0/29"}]},
        }]
        self.assertEqual(install.docker_network_pool_conflicts(managed), [])
        managed[1]["Internal"] = False
        self.assertTrue(install.docker_network_pool_conflicts(managed))

    def test_network_inventory_probe_failure_is_explicit(self):
        responses = [Mock(returncode=0, stdout="network-id\n"),
                     Mock(returncode=1, stdout="")]
        with patch("installer.install.docker_available", return_value=True), \
                patch("installer.install._command", side_effect=responses):
            self.assertIsNone(install.docker_network_inventory())

    def test_label_and_sizing_helpers(self):
        labels = {"com.docker.compose.project": "abc-123", "farm.release": "deadbeef"}
        self.assertEqual(install.compose_project_from_labels(labels), "abc-123")
        self.assertEqual(install.anchor_release_from_labels(labels), "deadbeef")
        self.assertIsNone(install.compose_project_from_labels(
            {"com.docker.compose.project": "unsafe project"}))
        size = install.sizing_summary({"active_capacity": 12, "catalog_capacity": 8}, 70, 3)
        self.assertEqual(size["calculated_active_limit"], 12)
        self.assertEqual(size["available_active_slots"], 9)
        self.assertEqual(size["safe_additional_catalog_devices"], 8)

    def test_environment_merge_preserves_image_pins_only(self):
        values = install.build_env(
            {"REDROID_IMAGE": "redroid@sha256:abc", "UNSAFE": "ignored"},
            farm_domain="farm.example.com", console_domain="console.example.com",
            network="coolify-prod", secret_dir=Path("/secure/secrets"), release_id="abc123")
        self.assertEqual(values["REDROID_IMAGE"], "redroid@sha256:abc")
        self.assertNotIn("UNSAFE", values)
        self.assertEqual(values["FARM_HTTP_BIND"], "127.0.0.1")
        self.assertEqual(values["FARM_HTTP_PORT"], "18080")
        self.assertEqual(values["FARM_TRAEFIK_ENABLED"], "true")
        self.assertEqual(values["GRAFANA_ROOT_URL"], "https://metrics.farm.example.com/")
        self.assertEqual(values["GRAFANA_SERVE_FROM_SUB_PATH"], "false")
        self.assertEqual(install.parse_env(install.render_env(values)), values)

    def test_ip_environment_and_provisioner_origin_do_not_invent_dns(self):
        paths = install.Paths()
        values = install.build_env(
            {"GRAFANA_DOMAIN": "metrics.old.example.com"},
            farm_domain="10.20.30.40", console_domain="10.20.30.40",
            network="coolify", secret_dir=Path("/secure/secrets"), release_id="abc123",
            access_mode="ip", public_ip="10.20.30.40", http_port=18080,
            auth_file=Path("/private/farm-users.htpasswd"),
        )
        self.assertEqual(values["FARM_DOMAIN"], "10.20.30.40")
        self.assertEqual(values["CONSOLE_DOMAIN"], "10.20.30.40")
        self.assertEqual(values["GRAFANA_DOMAIN"], "10.20.30.40")
        self.assertNotIn("metrics.10.20.30.40", values.values())
        self.assertEqual(values["FARM_HTTP_BIND"], "0.0.0.0")
        self.assertEqual(values["FARM_HTTP_PORT"], "18080")
        self.assertEqual(values["FARM_HTTP_AUTH_FILE"],
                         str(Path("/private/farm-users.htpasswd")))
        self.assertEqual(values["FARM_TRAEFIK_ENABLED"], "false")
        self.assertEqual(values["GRAFANA_ROOT_URL"], "http://10.20.30.40:18080/metrics/")
        self.assertEqual(values["GRAFANA_SERVE_FROM_SUB_PATH"], "true")
        switched = install.build_env(
            values, farm_domain="new.example.com", console_domain="console.new.example.com",
            network="coolify", secret_dir=Path("/secure/secrets"), release_id="next",
        )
        self.assertEqual(switched["GRAFANA_DOMAIN"], "metrics.new.example.com")
        self.assertEqual(switched["GRAFANA_ROOT_URL"], "https://metrics.new.example.com/")
        custom = install.build_env(
            {**values, "GRAFANA_DOMAIN": "observe.custom.example"},
            farm_domain="new.example.com", console_domain="console.new.example.com",
            network="coolify", secret_dir=Path("/secure/secrets"), release_id="next",
        )
        self.assertEqual(custom["GRAFANA_DOMAIN"], "observe.custom.example")
        self.assertEqual(custom["GRAFANA_ROOT_URL"], "https://observe.custom.example/")
        config = install.build_provisioner_config(
            {}, release_dir=Path("/opt/android-farm/releases/" + "a" * 16),
            project="android-farm-runtime", farm_domain="10.20.30.40", paths=paths,
            access_mode="ip", public_ip="10.20.30.40", http_port=18080,
        )
        self.assertEqual(config["access_mode"], "ip")
        self.assertEqual(config["console_url"], "http://10.20.30.40:18080")

    def test_busy_http_port_allows_only_attested_running_gateway(self):
        gateway = [{
            "Name": "/android-farm-gateway",
            "State": {"Running": True},
            "Config": {"Labels": {"farm.stack": "core", "farm.role": "gateway"}},
            "NetworkSettings": {"Ports": {"8080/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "18080"}
            ]}},
        }]
        docker_ps = Mock(returncode=0, stdout="gateway-id\n")
        with patch("installer.install._json_command", return_value=gateway), \
                patch("installer.install._command", return_value=docker_ps):
            self.assertTrue(install.http_port_available(18080, "127.0.0.1"))
            self.assertTrue(install.http_port_available(18080, "0.0.0.0"))
        listener = Mock()
        listener.bind.side_effect = OSError("in use")
        with patch("installer.install._json_command", return_value=None), \
                patch("installer.install._command", return_value=Mock(returncode=0, stdout="")), \
                patch("installer.install.socket.socket", return_value=listener):
            self.assertFalse(install.http_port_available(18080, "127.0.0.1"))
            listener.close.assert_called_once()

    def test_domain_port_ignores_other_nic_but_ip_widening_rejects_it(self):
        gateway = [{
            "Name": "/android-farm-gateway", "State": {"Running": True},
            "Config": {"Labels": {"farm.stack": "core", "farm.role": "gateway"}},
            "NetworkSettings": {"Ports": {"8080/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": "18080"}
            ]}},
        }]
        other = [{
            "Name": "/other", "State": {"Running": True}, "Config": {"Labels": {}},
            "NetworkSettings": {"Ports": {"8080/tcp": [
                {"HostIp": "10.20.30.40", "HostPort": "18080"}
            ]}},
        }]

        def json_command(argv):
            return gateway if argv == ["docker", "inspect", "android-farm-gateway"] else other

        docker_ps = Mock(returncode=0, stdout="other-id\n")
        with patch("installer.install._json_command", side_effect=json_command), \
                patch("installer.install._command", return_value=docker_ps):
            self.assertTrue(install.http_port_available(18080, "127.0.0.1"))
            self.assertFalse(install.http_port_available(18080, "0.0.0.0"))

        def host_listener_command(argv, **_kwargs):
            if argv[:2] == ["docker", "ps"]:
                return Mock(returncode=0, stdout="gateway-id\n")
            return Mock(returncode=0, stdout=(
                "LISTEN 0 4096 127.0.0.1:18080 0.0.0.0:*\n"
                "LISTEN 0 4096 10.20.30.40:18080 0.0.0.0:*\n"
            ))

        with patch("installer.install._json_command", return_value=gateway), \
                patch("installer.install._command", side_effect=host_listener_command):
            self.assertTrue(install.http_port_available(18080, "127.0.0.1"))
            self.assertFalse(install.http_port_available(18080, "0.0.0.0"))


class InstallerFilesystemTests(unittest.TestCase):
    @staticmethod
    def make_source(root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        for entry in install.RELEASE_ENTRIES:
            path = source / entry
            if Path(entry).suffix or entry.startswith("."):
                path.parent.mkdir(parents=True, exist_ok=True)
                if entry == "generate_farm.py":
                    path.write_text(
                        "def generate(count):\n"
                        "    return {'services': {f'android-num{i:02d}': {} for i in range(1, count + 1)}}\n",
                        encoding="utf-8")
                else:
                    path.write_text(f"content:{entry}\n", encoding="utf-8")
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / "keep.txt").write_text(f"content:{entry}\n", encoding="utf-8")
        # provisioner must be a file even though it has a .py suffix (covered above).
        return source

    def settings(self, root: Path, source: Path) -> install.Settings:
        dynamic = root / "dynamic"
        dynamic.mkdir(exist_ok=True)
        users = dynamic / "farm-users.htpasswd"
        if not users.exists():
            users.write_text("operator:$2y$05$fixture\n", encoding="utf-8")
            users.chmod(0o600)
        paths = install.Paths(
            release_root=root / "releases", config_dir=root / "etc",
            state_dir=root / "state", backup_dir=root / "backups",
            data_root=root / "data", wrapper=root / "bin" / "device-provisioner",
            traefik_dynamic_dir=dynamic)
        return install.Settings(source, "farm.example.com", "console.example.com",
                                "coolify", "project", paths, True)

    def test_release_copy_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            first, release_id, created = install.install_release(source, root / "releases")
            self.assertTrue(created)
            second, same_id, created_again = install.install_release(source, root / "releases")
            self.assertFalse(created_again)
            self.assertEqual((first, release_id), (second, same_id))
            self.assertFalse(first.is_symlink())
            manifest = json.loads((first / ".install-manifest.json").read_text())
            self.assertEqual(manifest["release_id"], release_id)
            valid, digest = install.verify_release_directory(first)
            self.assertTrue(valid, digest)

    def test_traefik_auth_is_installed_without_password_in_argv(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            traefik = source / 'traefik'
            (traefik / 'farm-auth.yml').write_text('http: {}\n', encoding='utf-8')
            base = self.settings(root, source)
            dynamic = root / 'dynamic'
            dynamic.mkdir(exist_ok=True)
            (dynamic / 'farm-users.htpasswd').unlink(missing_ok=True)
            password = root / 'password'
            password.write_text('strong secret\n', encoding='utf-8')
            password.chmod(0o600)
            settings = replace(base, paths=replace(base.paths, traefik_dynamic_dir=dynamic),
                               auth_user='operator', auth_password_file=password)
            completed = Mock(returncode=0, stdout='operator:$2y$05$example\n')
            with patch('installer.install._command', return_value=completed) as command:
                result = install.configure_traefik_auth(settings)
            self.assertEqual(command.call_args.args[0], ['htpasswd', '-niB', 'operator'])
            self.assertNotIn('strong secret', repr(command.call_args.args))
            self.assertEqual((dynamic / 'farm-auth.yml').read_text(), 'http: {}\n')
            self.assertEqual((dynamic / 'farm-users.htpasswd').read_text(), completed.stdout)
            self.assertEqual(Path(result['users_file']).resolve(),
                             (dynamic / 'farm-users.htpasswd').resolve())
            original = (dynamic / 'farm-users.htpasswd').read_bytes()
            original_revision = result['revision']

            verified = Mock(returncode=0, stdout='', stderr='')
            with patch('installer.install._command', return_value=verified) as command:
                same = install.configure_traefik_auth(settings)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0],
                             ['htpasswd', '-vi',
                              str((dynamic / 'farm-users.htpasswd').resolve()), 'operator'])
            self.assertNotIn('strong secret', repr(command.call_args.args))
            self.assertEqual((dynamic / 'farm-users.htpasswd').read_bytes(), original)
            self.assertEqual(same['revision'], original_revision)

            password.write_text('rotated secret\n', encoding='utf-8')
            password.chmod(0o600)
            mismatch = Mock(returncode=3, stdout='', stderr='')
            replacement = Mock(returncode=0, stdout='operator:$2y$05$replacement\n')
            with patch('installer.install._command', side_effect=[mismatch, replacement]) as command:
                rotated = install.configure_traefik_auth(settings)
            self.assertEqual(command.call_count, 2)
            self.assertTrue(all('rotated secret' not in repr(item.args)
                                for item in command.call_args_list))
            self.assertNotEqual((dynamic / 'farm-users.htpasswd').read_bytes(), original)
            self.assertNotEqual(rotated['revision'], original_revision)

            previous_env = install.build_env(
                {}, farm_domain='farm.example.com', console_domain='console.example.com',
                network='coolify', secret_dir=root / 'secrets', release_id='old',
                auth_revision=original_revision,
            )
            rotated_env = install.build_env(
                previous_env, farm_domain='farm.example.com', console_domain='console.example.com',
                network='coolify', secret_dir=root / 'secrets', release_id='old',
                auth_revision=rotated['revision'],
            )
            self.assertNotEqual(previous_env['FARM_HTTP_AUTH_REVISION'],
                                rotated_env['FARM_HTTP_AUTH_REVISION'])

    def test_release_renders_catalog_from_selected_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            release, _, _ = install.install_release(source, root / "releases", catalog_count=3)
            rendered = json.loads((release / "docker-compose.farm.yml").read_text())
            self.assertEqual(len(rendered["services"]), 3)

    def test_release_verification_detects_later_modification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            release, _, _ = install.install_release(source, root / "releases")
            target = release / "provisioner.py"
            target.chmod(0o600)
            target.write_text("modified\n", encoding="utf-8")
            valid, detail = install.verify_release_directory(release)
            self.assertFalse(valid)
            self.assertIn("differs", detail)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_release_rejects_symlink_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            target = source / "ops" / "keep.txt"
            target.unlink()
            os.symlink(source / "provisioner.py", target)
            with self.assertRaises(RuntimeError):
                install.release_digest(source)

    def test_managed_install_preserves_operator_state_and_finalizes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            settings = self.settings(root, source)
            settings.paths.config_dir.mkdir(parents=True)
            settings.paths.state_dir.mkdir(parents=True)
            secret = settings.paths.config_dir / "secrets"
            secret.mkdir()
            (secret / "num01.json").write_text("do-not-change", encoding="utf-8")
            trust = settings.paths.config_dir / "apk-trust.json"
            trust.write_text('{"packages":{"trusted":{}}}\n', encoding="utf-8")
            inventory = settings.paths.state_dir / "inventory.json"
            inventory.write_text('{"devices":{"num01":{}}}\n', encoding="utf-8")
            detected = {"coolify_network": "coolify", "compose_project": "coolify-project",
                        "anchor_present": True, "anchor_release": None}
            first = install.install_managed_files(settings, detected)
            second = install.install_managed_files(settings, detected)
            self.assertTrue(first["configured"])
            self.assertFalse(second["release_created"])
            self.assertEqual((secret / "num01.json").read_text(), "do-not-change")
            self.assertEqual(json.loads(trust.read_text())["packages"], {"trusted": {}})
            self.assertEqual(json.loads(inventory.read_text())["devices"], {"num01": {}})
            grafana_secret = settings.paths.config_dir / "monitoring" / "grafana-admin-password"
            self.assertTrue(grafana_secret.is_file())
            self.assertGreaterEqual(len(grafana_secret.read_text().strip()), 40)
            config = json.loads((settings.paths.config_dir / "provisioner.json").read_text())
            self.assertEqual(config["compose_project"], install.RUNTIME_COMPOSE_PROJECT)
            self.assertEqual(config["proxy_store_dir"], str(settings.paths.config_dir / "proxies"))
            wrapper = settings.paths.wrapper.read_text()
            self.assertIn(first["release_dir"], wrapper)
            self.assertNotIn("/current/", wrapper)

    def test_ip_install_persists_mode_port_and_browser_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            settings = replace(
                self.settings(root, source), farm_domain="10.20.30.40",
                console_domain="10.20.30.40", access_mode="ip",
                public_ip="10.20.30.40", http_port=18080,
            )
            result = install.install_managed_files(settings, {
                "coolify_network": "coolify", "compose_project": "coolify-project",
                "anchor_present": True, "anchor_release": None,
            })
            self.assertTrue(result["configured"])
            state = json.loads((settings.paths.state_dir / "install-state.json").read_text())
            self.assertEqual(state["access_mode"], "ip")
            self.assertEqual(state["public_ip"], "10.20.30.40")
            self.assertEqual(state["http_port"], 18080)
            config = json.loads((settings.paths.config_dir / "provisioner.json").read_text())
            self.assertEqual(config["access_mode"], "ip")
            self.assertEqual(config["console_url"], "http://10.20.30.40:18080")
            environment = install.parse_env(
                (settings.paths.config_dir / "compose.env").read_text()
            )
            self.assertEqual(environment["FARM_HTTP_BIND"], "0.0.0.0")
            self.assertEqual(environment["GRAFANA_DOMAIN"], "10.20.30.40")
            self.assertEqual(
                environment["FARM_HTTP_AUTH_REVISION"],
                install.auth_file_revision(
                    settings.paths.traefik_dynamic_dir / "farm-users.htpasswd"
                ),
            )

    def test_install_waits_for_anchor_without_creating_live_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = self.settings(root, self.make_source(root))
            result = install.install_managed_files(
                settings, {"coolify_network": "coolify", "compose_project": None,
                           "anchor_present": False, "anchor_release": None})
            self.assertFalse(result["configured"])
            self.assertEqual(result["status"], "waiting_for_coolify")
            self.assertFalse((settings.paths.config_dir / "provisioner.json").exists())
            self.assertFalse(settings.paths.wrapper.exists())
            self.assertTrue((settings.paths.config_dir / "compose.env").exists())
            self.assertTrue((settings.paths.config_dir / "coolify.env").exists())

    def test_doctor_checks_binderfs_without_mounting_or_requiring_host_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = self.settings(root, self.make_source(root))
            for available in (True, False):
                with self.subTest(available=available), \
                        patch("installer.install.binderfs_available", return_value=available), \
                        patch("installer.install.probe_binderfs") as probe:
                    checks = install.doctor_checks(settings, {})
                binder = next(item for item in checks if item.name == "binder")
                self.assertEqual(binder.status, "pass" if available else "block")
                self.assertIn("BinderFS", binder.detail)
                probe.assert_not_called()

    def test_doctor_never_reports_waiting_upgrade_as_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            settings = self.settings(root, source)
            settings.paths.state_dir.mkdir(parents=True)
            settings.paths.config_dir.mkdir(parents=True)
            (settings.paths.state_dir / "install-state.json").write_text(
                json.dumps({
                    "status": "waiting_for_coolify",
                    "waiting_reason": "active devices must stop",
                    "release_id": "a" * 16,
                    "release_dir": str(settings.paths.release_root / ("a" * 16)),
                }), encoding="utf-8")
            (settings.paths.config_dir / "provisioner.json").write_text(
                json.dumps({"compose_file": str(settings.paths.release_root / ("b" * 16) /
                                                        "docker-compose.farm.yml")}),
                encoding="utf-8")
            (settings.paths.config_dir / "compose.env").write_text("FARM_DOMAIN=farm.example.com\n",
                                                                     encoding="utf-8")
            checks = install.doctor_checks(settings, {
                "os_release": {"ID": "ubuntu", "VERSION_ID": "24.04"},
                "docker_ready": True,
                "docker_endpoint": "unix:///var/run/docker.sock",
                "compose_version": "2.33.1",
                "cgroup_v2": True,
                "docker_user_chain": True,
                "coolify_network": "coolify",
                "networks": ["coolify"],
                "network_inventory_ready": True,
                "network_pool_conflicts": [],
                "compose_project": "project",
                "anchor_present": True,
                "project_mismatch": False,
                "anchor_release": "a" * 16,
                "sizing": {"calculated_active_limit": 10},
            })
            state = next(item for item in checks if item.name == "installer-state")
            self.assertEqual(state.status, "warn")
            self.assertIn("active devices", state.detail)

    def test_staged_upgrade_does_not_change_live_config_on_anchor_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            settings = self.settings(root, source)
            first = install.install_managed_files(
                settings, {"coolify_network": "coolify", "compose_project": "project",
                           "anchor_present": True, "anchor_release": None, "sizing": {}})
            live_env = (settings.paths.config_dir / "compose.env").read_bytes()
            live_config = (settings.paths.config_dir / "provisioner.json").read_bytes()
            live_wrapper = settings.paths.wrapper.read_bytes()
            source.joinpath("provisioner.py").write_text("new release\n", encoding="utf-8")
            staged = install.install_managed_files(
                settings, {"coolify_network": "coolify", "compose_project": "project",
                           "anchor_present": True,
                           "anchor_release": first["release_id"], "sizing": {}})
            self.assertFalse(staged["configured"])
            self.assertNotEqual(staged["release_id"], first["release_id"])
            self.assertEqual((settings.paths.config_dir / "compose.env").read_bytes(), live_env)
            self.assertEqual((settings.paths.config_dir / "provisioner.json").read_bytes(), live_config)
            self.assertEqual(settings.paths.wrapper.read_bytes(), live_wrapper)
            staged_env = install.parse_env((settings.paths.config_dir / "coolify.env").read_text())
            self.assertEqual(staged_env["FARM_RELEASE_ID"], staged["release_id"])

    def test_matching_upgrade_waits_until_active_devices_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            settings = self.settings(root, source)
            first = install.install_managed_files(
                settings, {"coolify_network": "coolify", "compose_project": "project",
                           "anchor_present": True, "anchor_release": None,
                           "sizing": {"active_devices": 0}})
            old_wrapper = settings.paths.wrapper.read_bytes()
            source.joinpath("provisioner.py").write_text("new release\n", encoding="utf-8")
            new_digest, _ = install.release_digest(source, first["catalog_count"])
            staged = install.install_managed_files(
                settings, {"coolify_network": "coolify", "compose_project": "project",
                           "anchor_present": True, "anchor_release": new_digest[:16],
                           "sizing": {"active_devices": 2}})
            self.assertFalse(staged["configured"])
            self.assertIn("2 Android", staged["waiting_reason"])
            self.assertEqual(settings.paths.wrapper.read_bytes(), old_wrapper)

    def test_auto_catalog_never_shrinks_and_accounts_for_allocations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.make_source(root)
            settings = self.settings(root, source)
            settings.paths.state_dir.mkdir(parents=True)
            (settings.paths.state_dir / "install-state.json").write_text(
                json.dumps({"catalog_count": 70}), encoding="utf-8")
            (settings.paths.state_dir / "inventory.json").write_text(
                json.dumps({"devices": {f"num{i:02d}": {} for i in range(1, 81)}}), encoding="utf-8")
            self.assertEqual(
                install.choose_catalog_count(settings, {"safe_additional_catalog_devices": 5}), 85)
            self.assertEqual(
                install.choose_catalog_count(settings, {"safe_additional_catalog_devices": 0}), 80)
            explicit_smaller = replace(settings, catalog_count=79)
            with self.assertRaisesRegex(RuntimeError, "cannot shrink"):
                install.choose_catalog_count(explicit_smaller, {})


if __name__ == "__main__":
    unittest.main()
