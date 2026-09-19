import io
from http.client import IncompleteRead
import json
import unittest
from urllib.error import HTTPError, URLError

from installer.coolify_api import (
    APP_NAME, COMPOSE_LOCATION, REPOSITORY, CoolifyClient, CoolifyError,
    MAX_RESPONSE_BYTES, _NoRedirect, normalize_url,
)


COMMIT = "a" * 40
INSTALLATION = "installation-one"


class FakeOpener:
    def __init__(self, response=b"{}", error=None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(self.response)


class StatefulClient(CoolifyClient):
    """Fake API uses the official bare-list and object response shapes."""
    def __init__(self):
        super().__init__("https://coolify.example.com", "test-token")
        self.calls = []
        self.projects = []
        self.environments = []
        self.apps = {}
        self.destinations = [{"uuid": "destination1", "server_uuid": "server1", "type": "standalone", "network": "coolify"}]
        self.servers = [{"uuid": "server1", "ip": "192.0.2.1", "name": "my host", "is_usable": True}]
        self.other_resources = []

    def _request(self, method, path, payload=None, *, query=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            if path == "/servers":
                return self.servers
            if path == "/resources":
                return list(self.apps.values()) + self.other_resources
            if path == "/servers/server1/resources":
                return [app for app in self.apps.values() if app.get("server_uuid") == "server1"] + self.other_resources
            if path == "/projects":
                return self.projects
            if path == "/projects/project1/environments":
                return self.environments
            if path == "/projects/project1/environment1":
                return {"applications": [app for app in self.apps.values() if app.get("environment_uuid") == "environment1"]}
            if path == "/servers/server1/destinations":
                return self.destinations
            if path.startswith("/applications/"):
                return self.apps[path.rsplit("/", 1)[1]]
            if path == "/deployments/deployment1":
                return {"deployment_uuid": "deployment1", "status": "finished"}
        if method == "POST":
            if path == "/projects":
                self.projects = [{"uuid": "project1", **payload}]
                return {"uuid": "project1"}
            if path == "/projects/project1/environments":
                self.environments = [{"uuid": "environment1", **payload}]
                return {"uuid": "environment1"}
            if path == "/applications/public":
                app = {"uuid": "app1", **payload, "git_repository": "mjpouladi/AndroidFarm",
                       "source_id": 0, "source_type": "App\\Models\\GithubApp"}
                self.apps["app1"] = app
                return {"uuid": "app1"}
            if path == "/deploy":
                return {"deployments": [{"resource_uuid": payload["uuid"], "deployment_uuid": "deployment1"}]}
        if method == "PATCH":
            if path.endswith("/envs/bulk"):
                return payload["data"]
            if path == "/applications/app1":
                self.apps["app1"].update(payload)
                return {"uuid": "app1"}
        raise AssertionError(f"Unexpected fake API call: {method} {path}")

    def provision(self, **kwargs):
        return self.ensure_core(server_uuid="server1", source_commit=COMMIT,
                                installation_id=INSTALLATION, **kwargs)


class CoolifyTransportTests(unittest.TestCase):
    def test_url_requires_https_or_literal_loopback_without_credentials(self):
        for value in ("http://coolify.example.com", "https://user:secret@host", "https://host/?token=abc",
                      "https://host/#token", "https://host/unexpected", "https://host:70000", "https://host\\attacker"):
            with self.subTest(value=value), self.assertRaises(CoolifyError):
                normalize_url(value)
        self.assertEqual(normalize_url("https://host/api/v1/"), "https://host/api/v1")
        self.assertEqual(normalize_url("http://127.0.0.1:8000"), "http://127.0.0.1:8000/api/v1")
        self.assertEqual(normalize_url("http://[::1]:8000"), "http://[::1]:8000/api/v1")

    def test_token_is_header_only_and_request_timeout_is_bounded(self):
        opener = FakeOpener(b"[]")
        client = CoolifyClient("https://coolify.example.com", "sensitive-token", timeout=15, opener=opener)
        client.list_servers()
        request, timeout = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer sensitive-token")
        self.assertNotIn("sensitive-token", request.full_url)
        self.assertIsNone(request.data)
        self.assertEqual(timeout, 15)
        with self.assertRaises(CoolifyError):
            CoolifyClient("https://host", "abc\r\nX: stolen")
        with self.assertRaises(CoolifyError):
            CoolifyClient("https://host", "abc", timeout=0)

    def test_http_and_transport_errors_cannot_echo_secrets(self):
        for error in (HTTPError("https://host/secret-token", 401, "secret-token", {}, io.BytesIO(b"secret-token")),
                      URLError("secret-token"), TimeoutError("secret-token"), IncompleteRead(b"secret-token")):
            client = CoolifyClient("https://host", "secret-token", opener=FakeOpener(error=error))
            with self.assertRaises(CoolifyError) as caught:
                client.list_servers()
            self.assertNotIn("secret-token", str(caught.exception))

    def test_redirects_are_refused_before_bearer_is_forwarded(self):
        with self.assertRaises(CoolifyError):
            _NoRedirect().redirect_request(None, None, 302, "found", {}, "https://attacker.example")

    def test_invalid_large_or_wrong_shape_responses_are_rejected(self):
        for response in (b"<html>login</html>", b"x" * (MAX_RESPONSE_BYTES + 1), b'{"data": []}'):
            client = CoolifyClient("https://host", "token", opener=FakeOpener(response))
            with self.assertRaises(CoolifyError):
                client.list_servers()

    def test_application_deployment_history_uses_official_shape_and_safe_fields(self):
        record = {"deployment_uuid": "deployment1", "commit": COMMIT, "status": "finished",
                  "created_at": "2026-09-19T10:00:00.000000Z", "is_api": True,
                  "pull_request_id": 0, "logs": "secret", "configuration_snapshot": {"token": "secret"}}
        opener = FakeOpener(json.dumps({"count": 1, "deployments": [record]}).encode())
        client = CoolifyClient("https://host", "token", opener=opener)
        history = client.list_application_deployments("app1", take=25, skip=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["commit"], COMMIT)
        self.assertEqual(history[0]["created_at"], record["created_at"])
        self.assertNotIn("logs", history[0])
        self.assertNotIn("configuration_snapshot", history[0])
        request, _ = opener.requests[0]
        self.assertEqual(request.full_url, "https://host/api/v1/deployments/applications/app1?skip=10&take=25")
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        for kwargs in ({"take": 101}, {"take": True}, {"skip": -1}):
            with self.assertRaises(CoolifyError):
                client.list_application_deployments("app1", **kwargs)
        invalid = CoolifyClient("https://host", "token", opener=FakeOpener(b"[]"))
        with self.assertRaises(CoolifyError):
            invalid.list_application_deployments("app1")


class CoolifyProvisioningTests(unittest.TestCase):
    def test_server_requires_exact_unambiguous_address_match(self):
        client = StatefulClient()
        self.assertEqual(client.select_server(addresses=["192.0.2.1"])["uuid"], "server1")
        self.assertEqual(client.select_server(server_uuid="server1")["uuid"], "server1")
        with self.assertRaises(CoolifyError):
            client.select_server(hostnames=["my host"])
        with self.assertRaises(CoolifyError):
            client.select_server()
        client.servers.append({"uuid": "server2", "ip": "192.0.2.1"})
        with self.assertRaises(CoolifyError):
            client.select_server(addresses=["192.0.2.1"])

    def test_unusable_or_swarm_servers_are_rejected(self):
        for fields in ({"is_usable": False}, {"settings": {"is_build_server": True}}, {"settings": {"is_swarm_manager": True}}):
            client = StatefulClient()
            client.servers[0].update(fields)
            with self.assertRaises(CoolifyError):
                client.select_server(server_uuid="server1")

    def test_creation_is_commit_pinned_raw_compose_and_does_not_deploy_early(self):
        client = StatefulClient()
        core = client.provision()
        self.assertEqual((core.app_uuid, core.environment_uuid), ("app1", "environment1"))
        self.assertTrue(core.created)
        app = client.apps["app1"]
        self.assertEqual(app["git_commit_sha"], COMMIT)
        self.assertEqual(app["docker_compose_location"], COMPOSE_LOCATION)
        create_payload = next(payload for method, path, payload in client.calls if path == "/applications/public")
        self.assertEqual(create_payload["git_repository"], REPOSITORY)
        self.assertTrue(app["is_raw_compose_deployment_enabled"])
        self.assertFalse(app["is_container_label_escape_enabled"])
        self.assertFalse(app["instant_deploy"])
        self.assertFalse(app["is_auto_deploy_enabled"])
        self.assertFalse(app["autogenerate_domain"])
        self.assertNotIn("ports_mappings", app)

    def test_retry_after_lost_create_response_reuses_resource_marker(self):
        client = StatefulClient()
        client.provision()
        client._managed_apps.clear()  # New invocation has only the persisted installation ID.
        result = client.provision()
        self.assertFalse(result.created)
        self.assertEqual(result.app_uuid, "app1")
        self.assertEqual(sum(path == "/applications/public" for _, path, _ in client.calls), 1)
        self.assertEqual(sum(method == "POST" and path == "/projects" for method, path, _ in client.calls), 1)

    def test_existing_manual_core_is_not_adopted_or_duplicated(self):
        client = StatefulClient()
        client.other_resources = [{"uuid": "manual-app", "name": APP_NAME, "description": "manual"}]
        with self.assertRaises(CoolifyError):
            client.provision()
        self.assertTrue(all(method == "GET" for method, _, _ in client.calls))

    def test_explicit_adoption_requires_matching_source_and_no_other_owner(self):
        client = StatefulClient()
        client.provision()
        client.apps["app1"]["description"] = "Manually installed from the guide"
        with self.assertRaises(CoolifyError):
            client.provision(app_uuid="app1")
        core = client.provision(app_uuid="app1", adopt_existing=True)
        self.assertFalse(core.created)
        self.assertEqual(client.apps["app1"]["description"], "android-farm-quickstart:" + INSTALLATION)
        client.apps["app1"]["description"] = "android-farm-quickstart:another-installation"
        with self.assertRaises(CoolifyError):
            client.provision(app_uuid="app1", adopt_existing=True)
        client.apps["app1"]["description"] = "manual"
        client.apps["app1"]["git_repository"] = "https://github.com/other/repository"
        with self.assertRaises(CoolifyError):
            client.provision(app_uuid="app1", adopt_existing=True)
        with self.assertRaises(CoolifyError):
            client.provision(adopt_existing=True)

    def test_drift_or_wrong_host_is_never_modified(self):
        for field, value in (("description", "another-owner"), ("git_repository", "https://github.com/other/repo"),
                             ("build_pack", "dockerfile"), ("docker_compose_location", "/other.yml"),
                             ("git_branch", "other"), ("server_uuid", "server2"), ("environment_uuid", "other-env")):
            with self.subTest(field=field):
                client = StatefulClient()
                client.provision()
                client.apps["app1"][field] = value
                client.calls.clear()
                with self.assertRaises(CoolifyError):
                    client.provision(app_uuid="app1")
                self.assertTrue(all(method == "GET" for method, _, _ in client.calls))

    def test_duplicate_installation_marker_fails_closed(self):
        client = StatefulClient()
        client.provision()
        client.apps["app2"] = {**client.apps["app1"], "uuid": "app2"}
        with self.assertRaises(CoolifyError):
            client.provision()

    def test_destination_must_match_server_network_and_standalone_type(self):
        for fields in ({"network": "unrelated"}, {"type": "swarm"}, {"server_uuid": "other"}):
            client = StatefulClient()
            client.destinations[0].update(fields)
            with self.assertRaises(CoolifyError):
                client.provision(destination_uuid="destination1")
            self.assertNotIn("app1", client.apps)

    def test_sync_and_deploy_require_verified_ownership_and_real_bulk_shape(self):
        client = StatefulClient()
        with self.assertRaises(CoolifyError):
            client.sync_environment("unrelated", {"FARM_DOMAIN": "farm.example.com"})
        with self.assertRaises(CoolifyError):
            client.deploy("unrelated")
        core = client.provision()
        client.sync_environment(core.app_uuid, {"FARM_RELEASE_ID": "release1", "FARM_DOMAIN": "farm.example.com"})
        method, path, payload = client.calls[-1]
        self.assertEqual((method, path), ("PATCH", "/applications/app1/envs/bulk"))
        self.assertEqual(payload["data"][0]["key"], "FARM_DOMAIN")
        self.assertTrue(payload["data"][0]["is_literal"])
        self.assertFalse(payload["data"][0]["is_preview"])
        self.assertEqual(client.deploy(core.app_uuid), "deployment1")
        self.assertEqual(client.deployment_status("deployment1")["status"], "finished")
        with self.assertRaises(CoolifyError):
            client.sync_environment(core.app_uuid, {"BAD": "line\ninjection"})

    def test_unpinned_commit_is_rejected_before_any_api_mutation(self):
        client = StatefulClient()
        with self.assertRaises(CoolifyError):
            client.ensure_core(server_uuid="server1", source_commit="main", installation_id=INSTALLATION)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
