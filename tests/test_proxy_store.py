import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ops.proxy_store import ProxyStore, curl_health_config
from ops.farmctl import validate_managed_proxy
from ops.secureio import atomic_json


class Result:
    def __init__(self, returncode=0, stdout="8.8.8.8\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ProxyStoreTests(unittest.TestCase):
    def store(self, root):
        return ProxyStore(root / "state" / "proxies.json", root / "secrets")

    def add(self, store, proxy_id="proxy-one", username="sticky-session-1", password="secret-one"):
        return store.add(proxy_id, label="Frankfurt A", proxy_type="socks5", server="8.8.8.8",
                         server_port=1080, username=username, password=password,
                         expected_egress_ip="8.8.8.8")

    def test_add_normalizes_type_and_keeps_credentials_out_of_metadata_and_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = self.store(root)
            public = self.add(store)
            self.assertEqual(public["type"], "socks")
            self.assertEqual(public["credential"], "s***1")
            self.assertNotIn("password", public)
            metadata = (root / "state" / "proxies.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-one", metadata)
            self.assertNotIn("sticky-session-1", metadata)
            secret = json.loads((root / "secrets" / "proxy-one.json").read_text(encoding="utf-8"))
            self.assertEqual(secret["password"], "secret-one")
            self.assertEqual(store.show("proxy-one"), store.list()[0])

    def test_rejects_invalid_ids_private_ips_and_duplicate_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(Path(folder))
            for invalid in ("../proxy", "UPPER", "ab", "proxy_name"):
                with self.assertRaises(ValueError):
                    self.add(store, proxy_id=invalid)
            with self.assertRaises(ValueError):
                store.add("proxy-private", label="Private", proxy_type="http", server="10.0.0.2",
                          server_port=8080, username="user", password="pass",
                          expected_egress_ip="8.8.8.8")
            self.add(store)
            with self.assertRaises(RuntimeError):
                self.add(store, proxy_id="proxy-two")

    def test_one_to_one_assignment_disable_and_delete_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(Path(folder))
            self.add(store)
            store.add("proxy-two", label="London B", proxy_type="http", server="1.1.1.1",
                      server_port=8080, username="sticky-2", password="secret-two",
                      expected_egress_ip="1.1.1.1")
            self.assertEqual(store.assign("proxy-one", "num01")["assigned_device"], "num01")
            with self.assertRaises(RuntimeError):
                store.assign("proxy-two", "num01")
            with self.assertRaises(RuntimeError):
                store.delete("proxy-one")
            store.disable("proxy-two")
            with self.assertRaises(RuntimeError):
                store.assign("proxy-two", "num02")
            self.assertEqual([item["id"] for item in store.list(include_disabled=False)], ["proxy-one"])
            self.assertEqual(store.enable("proxy-two")["state"], "enabled")
            store.unassign("proxy-one", "num01")
            store.delete("proxy-one")
            with self.assertRaises(KeyError):
                store.show("proxy-one")

    def test_password_rotation_preserves_identity_and_redacts_new_password(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = self.store(root)
            before = self.add(store)
            after = store.rotate_password("proxy-one", "new-secret")
            self.assertEqual(after["secret_version"], before["secret_version"] + 1)
            self.assertEqual(after["server"], before["server"])
            self.assertEqual(after["credential"], before["credential"])
            self.assertNotIn("new-secret", json.dumps(after))
            secret = json.loads((root / "secrets" / "proxy-one.json").read_text(encoding="utf-8"))
            self.assertEqual(secret["password"], "new-secret")
            self.assertEqual(secret["username"], "sticky-session-1")

    def test_guarded_start_rechecks_registry_assignment_and_installed_credential(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = self.store(root)
            self.add(store)
            store.assign("proxy-one", "num01")
            installed = root / "installed"
            atomic_json(installed / "num01.json", store.provisioning_secret("proxy-one"))
            record = {"proxy_id": "proxy-one", "expected_egress_ip": "8.8.8.8"}
            validate_managed_proxy("num01", record, installed, store.registry, store.secret_dir)
            store.disable("proxy-one")
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                validate_managed_proxy("num01", record, installed, store.registry, store.secret_dir)

    def test_health_check_passes_credentials_only_through_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(Path(folder))
            self.add(store, password='p\\"ass')
            calls = []

            def runner(argv, **kwargs):
                calls.append((argv, kwargs))
                return Result()

            result = store.check_health("proxy-one", runner=runner)
            self.assertTrue(result["healthy"])
            argv, options = calls[0]
            self.assertEqual(argv, ["curl", "--config", "-"])
            self.assertNotIn("sticky-session-1", " ".join(argv))
            self.assertNotIn('p\\"ass', " ".join(argv))
            self.assertIn("proxy-user", options["input"])
            self.assertIn("sticky-session-1", options["input"])
            self.assertEqual(store.show("proxy-one")["last_health"]["status"], "healthy")

    def test_health_check_records_mismatch_and_never_returns_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.store(Path(folder))
            self.add(store)
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                store.check_health("proxy-one", runner=lambda *_args, **_kwargs: Result(stdout="1.1.1.1"))
            health = store.show("proxy-one")["last_health"]
            self.assertEqual(health["status"], "unhealthy")
            self.assertEqual(health["observed_egress_ip"], "1.1.1.1")
            with self.assertRaisesRegex(RuntimeError, "curl proxy health check failed"):
                store.check_health("proxy-one", runner=lambda *_args, **_kwargs: Result(22, "", "password"))
            self.assertNotIn("password", json.dumps(store.show("proxy-one")))

    def test_curl_config_rejects_control_characters(self):
        secret = {"type": "http", "server": "8.8.8.8", "server_port": 8080,
                  "username": "user", "password": "bad\npassword"}
        with self.assertRaises(ValueError):
            curl_health_config(secret)


if __name__ == "__main__":
    unittest.main()
