#!/usr/bin/env python3
"""Generate dependency-free JSON (a valid YAML subset) for Docker Compose."""
import argparse
import json
from pathlib import Path


def generate(count=70):
    if not 1 <= count <= 200:
        raise ValueError('count must be between 1 and 200')
    doc = {'name': 'android-farm-devices', 'services': {
        'farm-anchor': {'image': 'alpine:3.21', 'container_name': 'farm-anchor',
                        'command': ['sleep', 'infinity'], 'restart': 'unless-stopped',
                        'network_mode': 'none', 'mem_limit': '32m', 'cpus': 0.1,
                        'labels': {'farm.stack': 'core', 'traefik.enable': 'false'}}},
        'networks': {'coolify': {'external': True, 'name': '${COOLIFY_NETWORK:-coolify}'}},
        'volumes': {}, 'secrets': {}}
    for i in range(1, count + 1):
        d = f'num{i:02d}'
        proxy, android, screen = (f'{kind}-{d}' for kind in ('proxy', 'android', 'screen'))
        labels = {'farm.device': d, 'farm.stack': 'devices'}
        def common(kind, ram, cpu):
            return {'container_name': f'{kind}-{d}', 'profiles': ['manual'],
                    'restart': 'no', 'mem_limit': ram, 'cpus': cpu,
                    'pids_limit': 2048 if kind == 'android' else 256,
                    'labels': dict(labels, **{'farm.role': kind, 'traefik.enable': 'false'}),
                    'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}}
        egress, control = f'egress-{d}', f'control-{d}'
        doc['networks'][egress] = {
            'name': f'farm-egress-{d}', 'driver': 'bridge',
            'driver_opts': {'com.docker.network.bridge.name': f'br-af{i:03d}'},
            'ipam': {'config': [{'subnet': f'10.231.{i}.0/29'}]}}
        doc['networks'][control] = {
            'name': f'farm-control-{d}', 'internal': True,
            'ipam': {'config': [{'subnet': f'10.232.{i}.0/29'}]}}
        secret = f'proxy-{d}'
        doc['secrets'][secret] = {'file': '${FARM_SECRETS_DIR:-/etc/android-farm/secrets}/' + d + '.json'}
        p = common('proxy', '256m', 0.5)
        p.update({'image': '${PROXY_IMAGE:-android-farm/proxy:1}',
                  'build': {'context': '.', 'dockerfile': 'images/proxy/Dockerfile'},
                  'cap_add': ['NET_ADMIN'], 'cap_drop': ['NET_RAW'],
                  'sysctls': {'net.ipv6.conf.all.disable_ipv6': '1'},
                  'environment': {'CONTROL_CIDR': f'10.232.{i}.0/29',
                                  'EGRESS_CIDR': f'10.231.{i}.0/29'},
                  'networks': {egress: {'ipv4_address': f'10.231.{i}.2', 'gw_priority': 1},
                               control: {'ipv4_address': f'10.232.{i}.2'}},
                  'ports': [f'127.0.0.1:{5550+i}:5555'],
                  'secrets': [{'source': secret, 'target': 'proxy.json'}],
                  'healthcheck': {'test': ['CMD', '/healthcheck.sh'], 'interval': '20s',
                                  'timeout': '10s', 'retries': 3, 'start_period': '15s'}})
        a = common('android', '4g', 4.0)
        a.update({'image': '${REDROID_IMAGE:-redroid/redroid:12.0.0-latest}',
                  'privileged': True, 'network_mode': f'service:{proxy}',
                  'depends_on': {proxy: {'condition': 'service_healthy'}},
                  'volumes': [f'redroid-data-{d}:/data'], 'stop_grace_period': '60s',
                  'command': ['androidboot.redroid_width=720', 'androidboot.redroid_height=1280',
                              'androidboot.redroid_dpi=240', 'androidboot.redroid_fps=20',
                              'androidboot.redroid_gpu_mode=guest',
                              'androidboot.use_memfd=true',
                              'androidboot.redroid_dns=1.1.1.1', f'androidboot.serialno=farm-{d}',
                              'ro.product.brand=redroid',
                              'ro.product.manufacturer=remote-android',
                              'ro.product.model=Redroid QA Phone']})
        s = common('screen', '1g', 1.5)
        s.update({'image': '${SCREEN_IMAGE:-android-farm/screen:1}',
                  'build': {'context': '.', 'dockerfile': 'images/screen/Dockerfile'},
                  'init': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
                  'environment': {'DEVICE_ID': d, 'ADB_TARGET': f'10.232.{i}.2:5555'},
                  'networks': {'coolify': {}, control: {'ipv4_address': f'10.232.{i}.3'}},
                  'depends_on': {android: {'condition': 'service_started'}},
                  'healthcheck': {'test': ['CMD', 'curl', '-fsS', 'http://127.0.0.1:6080/vnc.html'],
                                  'interval': '30s', 'timeout': '5s', 'retries': 3}})
        router = f'farm-{d}'
        s['labels'].update({
            'traefik.enable': 'true', 'traefik.docker.network': '${COOLIFY_NETWORK:-coolify}',
            f'traefik.http.routers.{router}.rule': f'Host(`${{FARM_DOMAIN:-farm.example.com}}`) && PathPrefix(`/d/{d}/`)',
            f'traefik.http.routers.{router}.entrypoints': 'https',
            f'traefik.http.routers.{router}.tls': 'true',
            f'traefik.http.routers.{router}.tls.certresolver': 'letsencrypt',
            f'traefik.http.routers.{router}.middlewares': f'farm-auth@file,{router}-strip',
            f'traefik.http.routers.{router}.service': router,
            f'traefik.http.middlewares.{router}-strip.stripprefix.prefixes': f'/d/{d}',
            f'traefik.http.services.{router}.loadbalancer.server.port': '6080'})
        doc['services'].update({proxy: p, android: a, screen: s})
        volume = f'redroid-data-{d}'
        doc['volumes'][volume] = {'external': True, 'name': volume}
    return doc


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=70)
    parser.add_argument('--output', default='docker-compose.farm.yml')
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(generate(args.count), indent=2) + '\n', encoding='utf-8')
    print(args.output)
