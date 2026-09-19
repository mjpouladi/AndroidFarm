"""Small, fail-closed Coolify v1 client for the QA core installation wizard.

Schema references (official):
https://coolify.io/docs/api/endpoints/applications/create-public-application
https://coolify.io/docs/api/endpoints/applications/update-envs-by-application-uuid
https://github.com/coollabsio/coolify/tree/main/app/Http/Controllers/Api

Only an application carrying this installation's ownership marker may be
changed. Requests do not retry mutations: an uncertain response is recovered by
running discovery again with the same persisted installation ID.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPException
import ipaddress
import json
import re
import socket
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


REPOSITORY = "https://github.com/mjpouladi/AndroidFarm"
COMPOSE_LOCATION = "/docker-compose.yml"
APP_NAME = "farm-core"
ENVIRONMENT = "production"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class CoolifyError(RuntimeError):
    """An actionable error which never contains a response body or a token."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CoolifyError("Coolify redirected the request. Enter its final API URL; token was not forwarded.")


@dataclass(frozen=True)
class CoreResource:
    app_uuid: str
    project_uuid: str
    server_uuid: str
    environment_uuid: str
    created: bool = False


def normalize_url(value: str) -> str:
    """Accept an origin or /api/v1; HTTP is allowed only on literal loopback."""
    try:
        parts = urlsplit(value.strip())
        hostname = parts.hostname
        parts.port  # Validate malformed/out-of-range ports before a request.
    except ValueError:
        raise CoolifyError("Invalid Coolify URL.") from None
    if not hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise CoolifyError("Coolify URL must have a host and must not contain credentials, query, or fragment.")
    if any(ord(char) < 33 for char in value.strip()) or "\\" in value:
        raise CoolifyError("Invalid Coolify URL.")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower() == "localhost"
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise CoolifyError("Use HTTPS for Coolify, or HTTP through a local loopback connection.")
    if parts.path.rstrip("/") not in ("", "/api/v1"):
        raise CoolifyError("Coolify URL must be its origin or end in /api/v1.")
    return urlunsplit((parts.scheme, parts.netloc, "/api/v1", "", ""))


def _identifier(value: object, label: str = "resource UUID") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise CoolifyError(f"Invalid or missing {label} in Coolify configuration/response.")
    return value


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise CoolifyError("Unexpected Coolify response; a JSON object was required.")
    return value


def _list(value: object) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CoolifyError("Unexpected Coolify response; a JSON resource list was required.")
    return value


def _host(value: object) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(text.strip("[]")))
    except ValueError:
        return text


class CoolifyClient:
    def __init__(self, url: str, token: str, *, timeout: float = 30, opener=None):
        self.url = normalize_url(url)
        if not token or len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise CoolifyError("Invalid Coolify API token.")
        if not 1 <= timeout <= 120:
            raise CoolifyError("Coolify request timeout must be between 1 and 120 seconds.")
        self._token = token
        self.timeout = timeout
        # Do not forward API credentials through environment-configured proxies.
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self._managed_apps: set[str] = set()

    def _request(self, method: str, path: str, payload: object = None, *, query: Mapping[str, int] | None = None):
        if not path.startswith("/") or "?" in path or "#" in path:
            raise CoolifyError("Invalid internal Coolify API path.")
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        url = self.url + path + ("?" + urlencode(query) if query else "")
        request = Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json", "Content-Type": "application/json",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            error.close()
            hint = " Check token permissions/API access." if error.code in (401, 403) else ""
            raise CoolifyError(f"Coolify API returned HTTP {error.code}.{hint} Check the resource in Coolify before rerunning.", status=error.code) from None
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException):
            raise CoolifyError("Coolify API connection failed or timed out. Rerun with the same saved installation state; a mutation may already have completed.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CoolifyError("Coolify API response exceeded the size limit.")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except (UnicodeError, ValueError):
            raise CoolifyError("Coolify API did not return valid JSON.") from None

    def list_servers(self) -> list[dict]:
        return _list(self._request("GET", "/servers"))

    def list_projects(self) -> list[dict]:
        return _list(self._request("GET", "/projects"))

    def list_resources(self) -> list[dict]:
        return _list(self._request("GET", "/resources"))

    def select_server(self, *, server_uuid: str | None = None, hostnames: Iterable[str] = (), addresses: Iterable[str] = ()) -> dict:
        """Require one exact UUID/address/hostname match; never choose the first server."""
        servers = self.list_servers()
        if server_uuid:
            identifier = _identifier(server_uuid, "server UUID")
            matches = [server for server in servers if server.get("uuid") == identifier]
        else:
            hints = {_host(value) for value in (*hostnames, *addresses) if value}
            if not hints:
                raise CoolifyError("Provide the local host address/name or an explicit Coolify server UUID.")
            # Coolify's ip field also permits hostnames. Its display name is not
            # an address and is intentionally not used for host identification.
            matches = [server for server in servers if _host(server.get("ip")) in hints]
        if len(matches) != 1:
            raise CoolifyError("The local host did not match exactly one Coolify server. Supply its server UUID explicitly.")
        server = matches[0]
        _identifier(server.get("uuid"), "server UUID")
        settings = server.get("settings") or {}
        if not isinstance(settings, dict):
            raise CoolifyError("Invalid Coolify server settings response.")
        if settings.get("is_build_server") or settings.get("is_swarm_manager") or settings.get("is_swarm_worker"):
            raise CoolifyError("The QA core requires a standalone resource server, not a build-only/Swarm server.")
        if server.get("is_usable") is False or server.get("is_reachable") is False or settings.get("is_usable") is False or settings.get("is_reachable") is False:
            raise CoolifyError("Coolify reports the selected server as unreachable or unusable. Validate the server in Coolify first.")
        return server

    def _destination(self, server_uuid: str, network: str, explicit: str | None) -> str | None:
        try:
            destinations = _list(self._request("GET", f"/servers/{server_uuid}/destinations"))
        except CoolifyError as error:
            # Older Coolify releases lack this discovery route. They still
            # reject ambiguous destinations in the create application endpoint.
            if error.status != 404:
                raise
            return _identifier(explicit, "destination UUID") if explicit else None
        matches = [item for item in destinations if item.get("type") == "standalone" and
                   (item.get("uuid") == explicit if explicit else item.get("network") == network)]
        if len(matches) != 1:
            raise CoolifyError("Coolify has no unique standalone destination for the configured Docker network. Supply the matching destination UUID.")
        destination = matches[0]
        if destination.get("server_uuid") != server_uuid or destination.get("network") != network:
            raise CoolifyError("The selected Coolify destination does not match this server and Docker network.")
        return _identifier(destination.get("uuid"), "destination UUID")

    def ensure_core(self, *, server_uuid: str, source_commit: str, installation_id: str,
                    app_uuid: str | None = None, project_name: str = "android-farm",
                    destination_uuid: str | None = None, network: str = "coolify",
                    adopt_existing: bool = False) -> CoreResource:
        """Create or safely resume the one pinned Git Compose core for this host."""
        server_uuid = _identifier(server_uuid, "server UUID")
        installation_id = _identifier(installation_id, "installation ID")
        explicit_app_uuid = app_uuid
        if adopt_existing and not explicit_app_uuid:
            raise CoolifyError("Adopting an existing application requires its explicit application UUID.")
        if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise CoolifyError("An exact 40-character Git commit SHA is required for the Coolify source.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", project_name):
            raise CoolifyError("Invalid Coolify project name.")
        marker = "android-farm-quickstart:" + installation_id
        server_resources = _list(self._request("GET", f"/servers/{server_uuid}/resources"))
        server_ids = {item.get("uuid") for item in server_resources}
        all_resources = self.list_resources()
        owned = [item for item in all_resources if item.get("description") == marker]
        if len(owned) > 1:
            raise CoolifyError("Multiple resources have this installation marker. Resolve the duplicate in Coolify before continuing.")
        if owned:
            found_uuid = _identifier(owned[0].get("uuid"))
            if app_uuid and found_uuid != app_uuid:
                raise CoolifyError("Saved application UUID and installation marker disagree.")
            app_uuid = found_uuid
        if app_uuid:
            app_uuid = _identifier(app_uuid)
            if app_uuid not in server_ids:
                raise CoolifyError("The saved application does not belong to the selected server.")
        collisions = [item for item in server_resources if item.get("name") == APP_NAME and item.get("uuid") != app_uuid]
        if collisions:
            raise CoolifyError("An existing farm-core resource on this server is not owned by this wizard. Use its original deployment workflow; it will not be overwritten or duplicated.")
        projects = [item for item in self.list_projects() if item.get("name") == project_name]
        if len(projects) > 1:
            raise CoolifyError("Multiple Coolify projects have the requested name. Rename the duplicate before continuing.")
        if projects:
            project_uuid = _identifier(projects[0].get("uuid"), "project UUID")
        else:
            if app_uuid:
                raise CoolifyError("The saved application's expected Coolify project no longer exists.")
            project_uuid = _identifier(_object(self._request("POST", "/projects", {"name": project_name, "description": "Android QA device farm"})).get("uuid"), "project UUID")
        environments = _list(self._request("GET", f"/projects/{project_uuid}/environments"))
        production = [item for item in environments if item.get("name") == ENVIRONMENT]
        if len(production) > 1:
            raise CoolifyError("The Coolify production environment is ambiguous.")
        if not production:
            if app_uuid:
                raise CoolifyError("The saved application's production environment no longer exists.")
            environment_uuid = _identifier(_object(self._request("POST", f"/projects/{project_uuid}/environments", {"name": ENVIRONMENT})).get("uuid"), "environment UUID")
        else:
            environment_uuid = _identifier(production[0].get("uuid"), "environment UUID")
        environment = _object(self._request("GET", f"/projects/{project_uuid}/{environment_uuid}"))
        applications = _list(environment.get("applications", []))
        if app_uuid and app_uuid not in {item.get("uuid") for item in applications}:
            raise CoolifyError("The saved application is outside the expected project/production environment.")
        if any(item.get("name") == APP_NAME and item.get("uuid") != app_uuid for item in applications):
            raise CoolifyError("The production environment already contains an unrelated farm-core application.")
        pinned_settings = {
            "git_commit_sha": source_commit, "is_auto_deploy_enabled": False,
            "is_raw_compose_deployment_enabled": True,
            "is_container_label_escape_enabled": False,
        }
        created = not bool(app_uuid)
        if app_uuid:
            application = self.application_status(app_uuid)
            repo = str(application.get("git_repository", "")).rstrip("/").removesuffix(".git")
            # Coolify normalizes a public github.com URL into owner/repository
            # and associates its built-in public GitHub source (ID 0).
            source_matches = repo == REPOSITORY or (
                repo == "mjpouladi/AndroidFarm" and application.get("source_id") == 0 and
                application.get("source_type") == "App\\Models\\GithubApp"
            )
            description = str(application.get("description") or "")
            owned_here = description == marker
            can_adopt = bool(adopt_existing and explicit_app_uuid == app_uuid and
                             not description.startswith("android-farm-quickstart:"))
            if not (owned_here or can_adopt) or not source_matches or application.get("build_pack") != "dockercompose" or application.get("docker_compose_location") != COMPOSE_LOCATION or application.get("git_branch") != "main":
                raise CoolifyError("The existing application does not match this installation's ownership, repository, branch, and Compose path; no settings were changed.")
            if can_adopt:
                pinned_settings["description"] = marker
            self._request("PATCH", f"/applications/{app_uuid}", pinned_settings)
        else:
            destination = self._destination(server_uuid, network, destination_uuid)
            payload = {
                "project_uuid": project_uuid, "server_uuid": server_uuid,
                "environment_uuid": environment_uuid, "git_repository": REPOSITORY,
                "git_branch": "main", "build_pack": "dockercompose", "name": APP_NAME,
                "description": marker, "docker_compose_location": COMPOSE_LOCATION,
                "instant_deploy": False, "autogenerate_domain": False,
                **pinned_settings,
            }
            if destination:
                payload["destination_uuid"] = destination
            app_uuid = _identifier(_object(self._request("POST", "/applications/public", payload)).get("uuid"))
        self._managed_apps.add(app_uuid)
        return CoreResource(app_uuid, project_uuid, server_uuid, environment_uuid, created)

    def _require_managed(self, app_uuid: str) -> str:
        app_uuid = _identifier(app_uuid)
        if app_uuid not in self._managed_apps:
            raise CoolifyError("Run ensure_core to validate application ownership before modifying or deploying it.")
        return app_uuid

    def sync_environment(self, app_uuid: str, values: Mapping[str, str]) -> None:
        """Upsert generated non-secret Compose variables; preserve unrelated keys."""
        app_uuid = self._require_managed(app_uuid)
        data = []
        for key, value in sorted(values.items()):
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
                raise CoolifyError("Invalid generated Coolify environment variable.")
            data.append({"key": key, "value": value, "is_preview": False,
                         "is_literal": True, "is_multiline": False,
                         "is_runtime": True, "is_buildtime": True})
        if data:
            self._request("PATCH", f"/applications/{app_uuid}/envs/bulk", {"data": data})

    def deploy(self, app_uuid: str) -> str:
        app_uuid = self._require_managed(app_uuid)
        response = _object(self._request("POST", "/deploy", {"uuid": app_uuid, "force": False}))
        deployments = _list(response.get("deployments"))
        matching = [item for item in deployments if item.get("resource_uuid") == app_uuid]
        if len(matching) != 1:
            raise CoolifyError("Coolify did not return one deployment for the managed application. Inspect its deployment history before retrying.")
        return _identifier(matching[0].get("deployment_uuid"), "deployment UUID")

    def deployment_status(self, deployment_uuid: str) -> dict:
        return _object(self._request("GET", f"/deployments/{_identifier(deployment_uuid, 'deployment UUID')}"))

    def list_application_deployments(self, app_uuid: str, *, take: int = 100, skip: int = 0) -> list[dict]:
        """Read newest-first deployment history without exposing logs/secrets.

        Official endpoint returns ``{count, deployments}``, with ``commit``
        (possibly HEAD until cloning), ISO ``created_at``, and statuses queued,
        in_progress, finished, failed, or cancelled-by-user. Pagination is
        explicit and bounded; callers may request subsequent pages via skip.
        """
        app_uuid = _identifier(app_uuid)
        if isinstance(take, bool) or not isinstance(take, int) or not 1 <= take <= 100:
            raise CoolifyError("Deployment page size must be between 1 and 100.")
        if isinstance(skip, bool) or not isinstance(skip, int) or not 0 <= skip <= 100000:
            raise CoolifyError("Invalid deployment history offset.")
        payload = _object(self._request("GET", f"/deployments/applications/{app_uuid}",
                                        query={"skip": skip, "take": take}))
        records = _list(payload.get("deployments"))
        safe_fields = {"deployment_uuid", "commit", "status", "created_at", "updated_at",
                       "finished_at", "is_api", "pull_request_id", "configuration_hash"}
        return [{key: value for key, value in record.items() if key in safe_fields} for record in records]

    def application_status(self, app_uuid: str) -> dict:
        return _object(self._request("GET", f"/applications/{_identifier(app_uuid)}"))
