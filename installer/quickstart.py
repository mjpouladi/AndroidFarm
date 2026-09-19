#!/usr/bin/env python3
"""Resumable, guided installation of the QA farm through the Coolify API.

The existing host installer remains the authority for release, network and
capacity checks. This entry point coordinates its two phases, Coolify, and the
host control-plane role without storing an API token.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer import install
from installer.coolify_api import CoolifyError
from ops.secureio import atomic_json, read_private_json, require_private_directory

STATE_FILE = Path("/var/lib/android-farm/quickstart.json")
REPOSITORY = "https://github.com/mjpouladi/AndroidFarm.git"
FAILED_DEPLOYMENTS = {"failed", "cancelled", "canceled", "cancelled-by-user"}
CORE_CONTAINERS = ("farm-anchor", "farm-console", "android-farm-prometheus",
                   "android-farm-node-exporter", "android-farm-cadvisor", "android-farm-grafana",
                   "android-farm-gateway")


def say(message: str) -> None:
    print(message, flush=True)


def prompt(label: str, default: str = "", *, reader: Callable = input) -> str:
    value = reader(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def require_host() -> None:
    if platform.system() != "Linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("این فرمان را با sudo روی خود سرور Ubuntu اجرا کنید.")
    release = install._host_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") not in install.SUPPORTED_UBUNTU:
        raise RuntimeError("این راه‌انداز به Ubuntu 22.04 یا 24.04 نیاز دارد.")
    if Path("/.dockerenv").exists():
        raise RuntimeError("راه‌انداز باید روی میزبان اجرا شود، نه داخل کانتینر.")


@contextmanager
def setup_lock():
    import fcntl
    with open("/run/lock/android-farm-setup.lock", "a", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("یک نصب دیگر در حال اجراست؛ همان پنجره را دنبال کنید.") from exc
        yield


def source_commit(source: Path, runner: Callable = subprocess.run) -> str:
    def git(*args: str) -> str:
        result = runner(["git", "-C", str(source), *args], text=True, capture_output=True,
                        timeout=30, check=True)
        return result.stdout.strip()

    if git("remote", "get-url", "origin").removesuffix(".git") != REPOSITORY.removesuffix(".git"):
        raise RuntimeError("مسیر source باید checkout مخزن رسمی AndroidFarm باشد.")
    if git("status", "--porcelain"):
        raise RuntimeError("source تغییر محلی دارد؛ نصب متوقف شد تا فایل‌های شما جایگزین نشوند.")
    commit = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("شناسهٔ معتبر commit در source پیدا نشد.")
    return commit


def load_state(path: Path = STATE_FILE) -> dict:
    if not path.exists():
        return {"schema_version": 1, "installation_id": str(uuid.uuid4()), "phase": "new"}
    state = read_private_json(path, "quickstart state")
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise RuntimeError("فایل وضعیت راه‌انداز معتبر نیست؛ آن را خودکار بازنویسی نمی‌کنم.")
    try:
        uuid.UUID(state["installation_id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("شناسهٔ نصب ذخیره‌شده معتبر نیست.") from exc
    return state


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    # Only public installation metadata is retained. Credentials are never part
    # of this schema, even if a caller accidentally adds them to its dictionary.
    allowed = {"schema_version", "installation_id", "phase", "farm_domain", "console_domain",
               "coolify_url", "server_uuid", "app_uuid", "source_commit", "release_id",
               "deployment_uuid", "deployment_commit", "deployment_env_hash", "deployment_finished",
               "deployment_requested", "deployment_before", "updated_at", "auth_user", "control_plane_release"}
    allowed.update({"access_mode", "public_ip", "http_port"})
    require_private_directory(path.parent, "quickstart state directory", create=True)
    atomic_json(path, {**{key: value for key, value in state.items() if key in allowed},
                       "updated_at": int(time.time())})


def prepare_auth(paths: install.Paths, state: dict) -> tuple[str | None, Path | None]:
    require_private_directory(paths.config_dir, "farm config directory", create=True)
    password_path = paths.config_dir / "web-login-password"
    if password_path.exists():
        install._private_password(password_path)
        return str(state.get("auth_user", "operator")), password_path
    # Preserve credentials installed by the advanced/manual path.
    users = paths.traefik_dynamic_dir / "farm-users.htpasswd"
    if users.exists():
        valid, _ = install.traefik_users_status(users)
        if not valid:
            raise RuntimeError("فایل ورود قبلی مجوز امن ندارد؛ نصب متوقف شد.")
        return None, None
    install._atomic_write(password_path, (secrets.token_urlsafe(30) + "\n").encode(), 0o600)
    state["auth_user"] = "operator"
    return "operator", password_path


def local_addresses(runner: Callable = subprocess.run) -> tuple[set[str], set[str]]:
    names = {socket.gethostname(), socket.getfqdn()}
    addresses = {"127.0.0.1", "::1"}
    try:
        result = runner(["ip", "-j", "address", "show"], text=True, capture_output=True,
                        timeout=10, check=True)
        for interface in json.loads(result.stdout):
            for address in interface.get("addr_info", []):
                addresses.add(address["local"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return names, addresses


def select_server(client, requested: str | None, *, reader: Callable = input) -> str:
    names, addresses = local_addresses()
    # Coolify's built-in localhost destination normally uses this Docker host
    # alias. Only consider it when contacting Coolify through local loopback.
    if urlsplit(client.url).hostname in {"127.0.0.1", "::1", "localhost"}:
        local = install._command(["docker", "inspect", "coolify", "--format", "{{.State.Running}}"])
        if local.returncode == 0 and local.stdout.strip() == "true":
            names.add("host.docker.internal")
    if requested:
        return client.select_server(server_uuid=requested)["uuid"]
    try:
        return client.select_server(hostnames=names, addresses=addresses)["uuid"]
    except CoolifyError as exc:
        if exc.status is not None:
            raise
        servers = client.list_servers()
        if not servers:
            raise RuntimeError("در این Coolify سروری قابل دسترس نیست؛ دسترسی توکن را بررسی کنید.")
        say("سرور همین میزبان را از فهرست Coolify انتخاب کنید:")
        for server in servers:
            say(f"  {server.get('uuid')}  {server.get('name', '')}  {server.get('ip', '')}")
        selected = prompt("UUID سرور جاری", reader=reader)
        return client.select_server(server_uuid=selected)["uuid"]


def wait_deployment(client, deployment_uuid: str, *, timeout: int = 2400,
                    clock: Callable = time.monotonic, sleep: Callable = time.sleep,
                    report: Callable = say) -> None:
    deadline = clock() + timeout
    while clock() < deadline:
        payload = client.deployment_status(deployment_uuid)
        status = str(payload.get("status", "unknown")).lower()
        if status == "finished":
            return
        if status in FAILED_DEPLOYMENTS:
            raise RuntimeError("Deploy در Coolify ناموفق شد. لاگ همان Deploy را در Coolify ببینید و فرمان نصب را دوباره اجرا کنید.")
        report(f"  Coolify: {status}؛ در انتظار تکمیل ساخت و استقرار…")
        sleep(15)
    raise RuntimeError("مهلت انتظار Deploy تمام شد؛ وضعیت ذخیره شد. با اجرای همان فرمان ادامه دهید.")


def wait_anchor(release_id: str, *, timeout: int = 120, clock: Callable = time.monotonic,
                sleep: Callable = time.sleep, labels: Callable = install.anchor_labels) -> None:
    deadline = clock() + timeout
    while clock() < deadline:
        current = labels()
        if (install.anchor_release_from_labels(current) == release_id and
                install.compose_project_from_labels(current)):
            return
        sleep(3)
    raise RuntimeError("Deploy تمام شد اما release متناظر روی همین میزبان پیدا نشد؛ UUID سرور و لاگ Coolify را بررسی کنید.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def web_auth_ready(url: str, *, allow_loopback: bool = False) -> bool:
    """Read-only smoke check; no credentials, redirect following, or TLS bypass."""
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "http":
        try:
            if not (allow_loopback and parsed.hostname == "127.0.0.1"):
                install.normalize_public_ip(parsed.hostname or "")
            install.normalize_http_port(parsed.port)
        except (ValueError, TypeError):
            return False
    elif parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(url, timeout=10):
            return False  # A successful unauthenticated response is not protected.
    except urllib.error.HTTPError as exc:
        return exc.code == 401 and exc.headers.get("WWW-Authenticate", "").lower().startswith("basic ")
    except (OSError, urllib.error.URLError):
        return False


def core_runtime_status(runner: Callable = subprocess.run) -> dict[str, bool]:
    status = {name: False for name in CORE_CONTAINERS}
    try:
        result = runner(["docker", "inspect", *CORE_CONTAINERS], text=True, capture_output=True,
                        timeout=30, check=False)
        if result.returncode:
            return status
        for item in json.loads(result.stdout):
            name = item.get("Name", "").lstrip("/")
            if name in status:
                current = item.get("State", {})
                status[name] = bool(current.get("Running") and not current.get("Restarting") and
                                    current.get("Health", {}).get("Status", "healthy") == "healthy")
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        return status
    return status


def deployment_needed(state: dict, commit: str, env_hash: str) -> bool:
    return not (state.get("deployment_uuid") and state.get("deployment_commit") == commit and
                state.get("deployment_env_hash") == env_hash)


def resume_deployments(client, state: dict, state_path: Path, *, retry_unknown: bool = False) -> None:
    """Resolve an uncertain POST before changing the app or enqueueing more work."""
    app_uuid = state.get("app_uuid")
    if not app_uuid:
        return
    deployments = client.list_application_deployments(app_uuid)
    if state.get("deployment_requested"):
        before = set(state.get("deployment_before", []))
        candidates = [item for item in deployments if item.get("deployment_uuid") not in before]
        if len(candidates) > 1:
            raise RuntimeError("چند Deploy پس از قطع نصب ایجاد شده است؛ ابهام را در Coolify رفع کنید. Deploy جدید ارسال نشد.")
        if candidates:
            candidate = candidates[0]
            source = str(candidate.get("commit", ""))
            if re.fullmatch(r"[0-9a-f]{40}", source) and source != state.get("deployment_commit"):
                raise RuntimeError("Deploy پیدا‌شده مربوط به commit دیگری است؛ تغییر خودکار متوقف شد.")
            state.update(deployment_uuid=candidate["deployment_uuid"], deployment_requested=False)
            save_state(state, state_path)
        elif retry_unknown:
            state.update(deployment_requested=False, deployment_uuid=None)
            save_state(state, state_path)
        else:
            raise RuntimeError("نتیجهٔ درخواست قبلی Deploy هنوز معلوم نیست. در Coolify وضعیت را بررسی کنید؛ اگر هیچ Deploy ایجاد نشده، همان فرمان را با --retry-deploy اجرا کنید.")
    # Never repin Git/ENV while this application's earlier deployment is active.
    for item in deployments:
        status = str(item.get("status", "unknown")).lower()
        if status not in {"finished", *FAILED_DEPLOYMENTS}:
            say("Deploy قبلی هنوز فعال است؛ پیش از تغییر تنظیمات منتظر آن می‌مانم…")
            wait_deployment(client, item["deployment_uuid"])


def run_setup(settings: install.Settings, client, state: dict, *, state_path: Path = STATE_FILE,
              commit: str, server_uuid: str, requested_app: str | None = None,
              control_installer: Callable | None = None, retry_deploy: bool = False) -> dict:
    if control_installer is None:
        from installer.control_plane import install_control_plane
        control_installer = install_control_plane
    say("۱/۵ — بررسی میزبان، ظرفیت، شبکه و آماده‌سازی امن…")
    if install.active_device_count():
        raise RuntimeError("برای نصب/ارتقا ابتدا دستگاه‌های فعال را با device-provisioner down خاموش کنید؛ داده‌ها حفظ می‌شوند.")
    existing_anchor = install.anchor_labels()
    if existing_anchor and not (state.get("app_uuid") or requested_app):
        raise RuntimeError("یک نصب قبلی در Coolify پیدا شد. برای جلوگیری از ساخت برنامهٔ تکراری، --app-uuid را با UUID همان برنامه اجرا کنید.")
    resume_state = ({**state, "app_uuid": requested_app} if requested_app and not state.get("app_uuid") else state)
    resume_deployments(client, resume_state, state_path, retry_unknown=retry_deploy)
    prepared = install.apply(settings)
    result = prepared["result"]
    state.update(phase="host_prepared", release_id=result["release_id"], source_commit=commit,
                 server_uuid=server_uuid)
    save_state(state, state_path)
    say("۲/۵ — ساخت یا بازیابی پروژه و برنامه در Coolify…")
    resource = client.ensure_core(server_uuid=server_uuid, source_commit=commit,
                                  installation_id=state["installation_id"],
                                  app_uuid=requested_app or state.get("app_uuid"),
                                  adopt_existing=bool(requested_app),
                                  network=prepared["detected"]["coolify_network"])
    state["app_uuid"] = resource.app_uuid
    save_state(state, state_path)
    env = install._existing_env(Path(result["coolify_env_file"]))
    client.sync_environment(resource.app_uuid, env)
    env_hash = hashlib.sha256(install.render_env(env).encode()).hexdigest()
    say("۳/۵ — استقرار هسته؛ ساخت نخستین نسخه ممکن است چند دقیقه طول بکشد…")
    need_deploy = deployment_needed(state, commit, env_hash)
    if not need_deploy:
        try:
            status = str(client.deployment_status(state["deployment_uuid"]).get("status", "unknown")).lower()
            need_deploy = status in FAILED_DEPLOYMENTS or (status == "finished" and not all(core_runtime_status().values()))
        except CoolifyError as exc:
            if exc.status != 404:
                raise
            need_deploy = True  # Old deployment history may have been pruned.
    if need_deploy:
        previous = client.list_application_deployments(resource.app_uuid)
        state.update(deployment_requested=True,
                     deployment_before=[item["deployment_uuid"] for item in previous],
                     deployment_uuid=None, deployment_commit=commit,
                     deployment_env_hash=env_hash, deployment_finished=False, phase="deploy_requested")
        save_state(state, state_path)
        deployment = client.deploy(resource.app_uuid)
        state.update(deployment_uuid=deployment, deployment_commit=commit,
                     deployment_env_hash=env_hash, deployment_requested=False,
                     deployment_finished=False, phase="deploying")
        save_state(state, state_path)
    wait_deployment(client, state["deployment_uuid"])
    state.update(deployment_finished=True, phase="core_deployed")
    save_state(state, state_path)
    wait_anchor(result["release_id"])
    say("۴/۵ — فعال‌سازی CLI، صف Redis و بررسی خودکار سلامت…")
    finalized = install.apply(settings)["result"]
    if not finalized["configured"]:
        raise RuntimeError("فعال‌سازی کامل نشد: " + str(finalized.get("waiting_reason", "unknown")))
    control_installer(Path(finalized["release_dir"]))
    state.update(phase="control_plane_ready", control_plane_release=finalized["release_id"])
    save_state(state, state_path)
    say("۵/۵ — بررسی نهایی میزبان و احراز هویت وب…")
    health = install.doctor(settings)
    core = core_runtime_status()
    urls = access_urls(settings, env)
    # Public NAT addresses may not hairpin back to this server. Probe the local
    # published port; outside firewall reachability remains an operator check.
    if settings.access_mode == "ip":
        web = {name: web_auth_ready(urlsplit(url)._replace(
            netloc=f"127.0.0.1:{settings.http_port}").geturl(), allow_loopback=True)
            for name, url in urls.items()}
    else:
        web = {name: web_auth_ready(url) for name, url in urls.items()}
    ready = health["status"] == "ready" and all(web.values()) and all(core.values())
    state["phase"] = "ready" if ready else "needs_attention"
    save_state(state, state_path)
    return {"ready": ready, "doctor": health, "web": web, "core": core, "urls": urls,
            "release_id": finalized["release_id"], "app_uuid": resource.app_uuid}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="نصب هدایت‌شدهٔ Android Farm در Coolify")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--domain", help="حالت پیش‌فرض HTTPS با دامنه؛ مثال: farm.example.com")
    access.add_argument("--ip", help="حالت اختیاری بدون دامنه با IPv4 سرور")
    parser.add_argument("--console-domain", help="دامنهٔ جداگانهٔ اختیاری پنل؛ پیش‌فرض همان دامنهٔ اصلی")
    parser.add_argument("--port", type=int, help="پورت درگاه HTTP؛ پیش‌فرض 18080، در حالت دامنه فقط loopback")
    parser.add_argument("--coolify-url", help="آدرس پنل Coolify؛ HTTPS یا HTTP فقط روی loopback")
    parser.add_argument("--token-file", type=Path, help="فایل خصوصی root:0600؛ در حالت عادی توکن مخفی پرسیده می‌شود")
    parser.add_argument("--server-uuid", help="برای میزبان‌های چندسروری یا NAT")
    parser.add_argument("--app-uuid", help="UUID برنامهٔ موجود همین نصب")
    parser.add_argument("--retry-deploy", action="store_true",
                        help="پس از بررسی دستی نبود Deploy در Coolify، درخواست نامعلوم قبلی را دوباره ارسال کن")
    return parser


def select_access(args, state: dict, initial: dict, *, reader: Callable = input) -> dict:
    """Default new installs to HTTPS; preserve the access mode of resumed installs."""
    saved = state if state.get("farm_domain") else initial
    saved_mode = saved.get("access_mode", "domain")
    mode = "domain" if args.domain else "ip" if args.ip else saved_mode
    port = install.normalize_http_port(args.port if args.port is not None else saved.get("http_port", 18080))
    if mode == "ip":
        if args.console_domain:
            raise ValueError("--console-domain فقط در حالت دامنه قابل استفاده است.")
        address = install.normalize_public_ip(args.ip or saved.get("public_ip") or
                    prompt("IPv4 سرور برای دسترسی مرورگر", reader=reader))
        return {"access_mode": mode, "public_ip": address, "http_port": port,
                "farm_domain": address, "console_domain": address}
    if mode != "domain":
        raise ValueError("حالت دسترسی ذخیره‌شده معتبر نیست.")
    domain = install.normalize_domain(args.domain or saved.get("farm_domain") or
                                      prompt("دامنهٔ فارم (بدون https)", reader=reader))
    console = args.console_domain or (saved.get("console_domain") if saved_mode == "domain" and
               saved.get("farm_domain") == domain else None) or domain
    return {"access_mode": mode, "public_ip": None, "http_port": port,
            "farm_domain": domain, "console_domain": install.normalize_domain(console)}


def access_urls(settings: install.Settings, env: dict | None = None) -> dict[str, str]:
    if settings.access_mode == "ip":
        origin = install.access_origin(access_mode="ip", farm_domain=settings.farm_domain,
                                       public_ip=settings.public_ip, http_port=settings.http_port)
        return {"console": origin + "/", "monitoring": origin + "/metrics/"}
    grafana = install.normalize_domain((env or {}).get("GRAFANA_DOMAIN", f"metrics.{settings.farm_domain}"))
    return {"console": f"https://{settings.console_domain}/", "monitoring": f"https://{grafana}/"}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        require_host()
        from installer.coolify_api import CoolifyClient
        with setup_lock():
            state = load_state()
            initial = install._read_json_if_regular(install.Paths.state_dir / "install-state.json")
            access = select_access(args, state, initial)
            url = args.coolify_url or state.get("coolify_url") or prompt(
                "آدرس Coolify", "http://127.0.0.1:8000")
            say("در Coolify، API Access را فعال کنید؛ سپس در Keys & Tokens → API Tokens یک توکن موقت root برای تیم این سرور بسازید.")
            say("راه‌انداز هم تنظیمات می‌سازد و هم Deploy می‌کند؛ توکن deploy-only کافی نیست. پس از پایان نصب می‌توانید توکن موقت را لغو کنید.")
            token = (install._private_password(args.token_file) if args.token_file else
                     getpass.getpass("توکن API (نمایش و ذخیره نمی‌شود): ").strip())
            if not token:
                raise ValueError("توکن API خالی است.")
            client = CoolifyClient(url, token)
            commit = source_commit(ROOT)
            server = select_server(client, args.server_uuid or state.get("server_uuid"))
            state.update(**access, coolify_url=url, server_uuid=server)
            save_state(state)
            paths = install.Paths()
            user, password = prepare_auth(paths, state)
            save_state(state)
            settings = install.Settings(ROOT, access["farm_domain"], access["console_domain"],
                                        "auto", "auto", paths, auth_user=user, auth_password_file=password,
                                        access_mode=access["access_mode"], public_ip=access["public_ip"],
                                        http_port=access["http_port"])
            if settings.access_mode == "ip":
                say(f"بدون نیاز به DNS: {access_urls(settings)['console']}؛ پورت TCP {settings.http_port} باید از شبکهٔ شما قابل دسترس باشد.")
                say("این حالت HTTP رمزگذاری نشده است؛ دسترسی را به شبکهٔ مطمئن یا VPN محدود کنید.")
            else:
                domains = list(dict.fromkeys([settings.farm_domain, settings.console_domain,
                                             f"metrics.{settings.farm_domain}"]))
                say("DNS این نام‌ها باید به همین سرور اشاره کند: " + "، ".join(domains))
            outcome = run_setup(settings, client, state, commit=commit, server_uuid=server,
                                requested_app=args.app_uuid, retry_deploy=args.retry_deploy)
            say("نصب تکمیل شد." if outcome["ready"] else "اجزای نصب آماده‌اند؛ موارد زیر هنوز نیاز به بررسی دارند:")
            for check in outcome["doctor"]["checks"]:
                if check["status"] != "pass":
                    say(f"  {check['name']}: {check['detail']} — {check.get('remediation') or ''}")
            for name, passed in outcome["web"].items():
                if not passed:
                    checks = "پورت/firewall/Basic Auth" if settings.access_mode == "ip" else "DNS/TLS/Basic Auth"
                    say(f"  {checks} را برای {outcome['urls'][name]} در Coolify بررسی کنید.")
            for name, passed in outcome["core"].items():
                if not passed:
                    say(f"  سرویس {name} هنوز سالم/فعال نیست؛ لاگ آن را در Coolify ببینید.")
            say(f"کنسول نمایشی: {outcome['urls']['console']}")
            say(f"مانیتورینگ: {outcome['urls']['monitoring']}")
            if settings.access_mode == "ip":
                say("بررسی وب از داخل میزبان انجام شد؛ دسترسی از شبکهٔ خودتان را با بازکردن لینک‌ها بررسی کنید.")
            say(f"برنامهٔ Coolify: {outcome['app_uuid']}")
            if password:
                say(f"نام کاربری وب: {user}؛ فایل خصوصی رمز: {password}")
                if sys.stdout.isatty():
                    say("رمز ورود وب (در password manager نگه دارید): " + install._private_password(password))
            else:
                say("ورود وب از همان حساب Basic Auth قبلی استفاده می‌کند.")
            say("عملیات واقعی: sudo device-provisioner status")
            say("برای ادامه پس از هر توقف: sudo bash /opt/android-farm/source/install.sh")
            return 0 if outcome["ready"] else 2
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, EOFError) as exc:
        # API client errors contain only sanitized status/context, never bodies.
        say(f"نصب کامل نشد: {exc}")
        say("وضعیت امن حفظ شده است؛ پس از رفع مورد، همان فرمان نصب را دوباره اجرا کنید.")
        return 1
    except KeyboardInterrupt:
        say("نصب متوقف شد. داده‌ها حفظ شده‌اند؛ با همان فرمان ادامه دهید.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
