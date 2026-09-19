#!/usr/bin/env python3
"""Generate dependency-free JSON (a valid YAML subset) for Docker Compose."""
import argparse
import json
from pathlib import Path

from ops.device_ids import DEVICE_LIMIT, device_id, network_plan


def _document(name='android-farm-runtime'):
    return {
        'name': name,
        'services': {},
        'networks': {'coolify': {'external': True, 'name': '${COOLIFY_NETWORK:-coolify}'}},
        'volumes': {},
        'secrets': {},
    }


def _add_device(doc, index):
    d = device_id(index)
    addresses = network_plan(index)
    proxy, android, screen = (f'{kind}-{d}' for kind in ('proxy', 'android', 'screen'))
    labels = {'farm.device': d, 'farm.stack': 'devices'}

    def common(kind, ram, cpu):
        return {
            'container_name': f'{kind}-{d}',
            'profiles': ['manual'],
            'restart': 'no',
            'mem_limit': ram,
            'cpus': cpu,
            'pids_limit': 2048 if kind == 'android' else 256,
            'labels': dict(labels, **{'farm.role': kind, 'traefik.enable': 'false'}),
            'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
        }

    egress, control = f'egress-{d}', f'control-{d}'
    doc['networks'][egress] = {
        'name': f'farm-egress-{d}',
        'driver': 'bridge',
        'driver_opts': {'com.docker.network.bridge.name': addresses['bridge']},
        'ipam': {'config': [{'subnet': addresses['egress_subnet']}]},
    }
    doc['networks'][control] = {
        'name': f'farm-control-{d}',
        'internal': True,
        'ipam': {'config': [{'subnet': addresses['control_subnet']}]},
    }
    secret = f'proxy-{d}'
    doc['secrets'][secret] = {
        'file': '${FARM_SECRETS_DIR:-/etc/android-farm/secrets}/' + d + '.json'
    }

    proxy_service = common('proxy', '256m', 0.5)
    proxy_service.update({
        'image': '${PROXY_IMAGE:-android-farm/proxy:1}',
        'build': {'context': '.', 'dockerfile': 'images/proxy/Dockerfile'},
        'cap_add': ['NET_ADMIN'],
        'cap_drop': ['NET_RAW'],
        'sysctls': {'net.ipv6.conf.all.disable_ipv6': '1'},
        'environment': {'CONTROL_CIDR': addresses['control_subnet'],
                        'EGRESS_CIDR': addresses['egress_subnet']},
        'networks': {
            egress: {'ipv4_address': addresses['proxy_egress_ip'], 'gw_priority': 1},
            control: {'ipv4_address': addresses['proxy_control_ip']},
        },
        'ports': [f"127.0.0.1:{addresses['adb_port']}:5555"],
        'secrets': [{'source': secret, 'target': 'proxy.json'}],
        'healthcheck': {'test': ['CMD', '/healthcheck.sh'], 'interval': '20s',
                        'timeout': '10s', 'retries': 3, 'start_period': '15s'},
    })

    android_service = common('android', '4g', 4.0)
    android_service.update({
        'image': '${REDROID_IMAGE:-redroid/redroid:12.0.0-latest}',
        'privileged': True,
        'network_mode': f'service:{proxy}',
        'depends_on': {proxy: {'condition': 'service_healthy'}},
        'volumes': [f'redroid-data-{d}:/data'],
        'stop_grace_period': '60s',
        'command': [
            'androidboot.redroid_width=720',
            'androidboot.redroid_height=1280',
            'androidboot.redroid_dpi=240',
            'androidboot.redroid_fps=20',
            'androidboot.redroid_gpu_mode=guest',
            'androidboot.use_memfd=true',
            'androidboot.redroid_dns=1.1.1.1',
            f'androidboot.serialno=farm-{d}',
            'ro.product.brand=redroid',
            'ro.product.manufacturer=remote-android',
            'ro.product.model=Redroid QA Phone',
        ],
    })

    screen_service = common('screen', '1g', 1.5)
    screen_service.update({
        'image': '${SCREEN_IMAGE:-android-farm/screen:1}',
        'build': {'context': '.', 'dockerfile': 'images/screen/Dockerfile'},
        'init': True,
        'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'],
        'environment': {
            'DEVICE_ID': d,
            'ADB_TARGET': f"{addresses['proxy_control_ip']}:5555",
            'SCREEN_WIDTH': '720',
            'SCREEN_HEIGHT': '1280',
            'SCREEN_FPS': '20',
        },
        'networks': {'coolify': {}, control: {'ipv4_address': addresses['screen_control_ip']}},
        'depends_on': {android: {'condition': 'service_started'}},
        'healthcheck': {
            'test': [
                'CMD-SHELL',
                'curl -fsS http://127.0.0.1:6080/vnc.html >/dev/null && '
                'test "$$(adb -s "$$ADB_TARGET" shell getprop sys.boot_completed 2>/dev/null)" = "1" && '
                'pgrep -x scrcpy >/dev/null',
            ],
            'interval': '30s',
            'timeout': '10s',
            'retries': 3,
            'start_period': '300s',
        },
    })
    router = f'farm-{d}'
    screen_service['labels'].update({
        'traefik.enable': '${FARM_TRAEFIK_ENABLED:-true}',
        'traefik.docker.network': '${COOLIFY_NETWORK:-coolify}',
        f'traefik.http.routers.{router}.rule': (
            f'Host(`${{FARM_DOMAIN:-farm.example.com}}`) && PathPrefix(`/d/{d}/`)'
        ),
        f'traefik.http.routers.{router}.entrypoints': 'https',
        f'traefik.http.routers.{router}.priority': '100',
        f'traefik.http.routers.{router}.tls': 'true',
        f'traefik.http.routers.{router}.tls.certresolver': 'letsencrypt',
        f'traefik.http.routers.{router}.middlewares': f'farm-auth@file,{router}-strip',
        f'traefik.http.routers.{router}.service': router,
        f'traefik.http.middlewares.{router}-strip.stripprefix.prefixes': f'/d/{d}',
        f'traefik.http.services.{router}.loadbalancer.server.port': '6080',
    })

    doc['services'].update({proxy: proxy_service, android: android_service, screen: screen_service})
    volume = f'redroid-data-{d}'
    doc['volumes'][volume] = {'external': True, 'name': volume}


def generate(count=1):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= DEVICE_LIMIT:
        raise ValueError(f'count must be between 1 and {DEVICE_LIMIT}')
    # Coolify owns only the core stack. Devices use a stable host-owned project,
    # so a Coolify redeploy cannot remove on-demand containers as orphans.
    doc = _document()
    for index in range(1, count + 1):
        _add_device(doc, index)
    return doc


def generate_one(index):
    """Render one slot in O(1), including its audited networks and volume."""
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= DEVICE_LIMIT:
        raise ValueError(f'index must be between 1 and {DEVICE_LIMIT}')
    d = device_id(index)
    doc = _document(f'android-farm-{d}')
    _add_device(doc, index)
    return doc


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, required=True,
                        help='catalog slots to pre-render; smart installer calculates this from storage')
    parser.add_argument('--output', default='docker-compose.farm.yml')
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(generate(args.count), indent=2) + '\n', encoding='utf-8')
    print(args.output)
