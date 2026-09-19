import ipaddress
import json
import os
from pathlib import Path


def make_config(secret):
    ip = ipaddress.IPv4Address(secret['server'])
    if not ip.is_global:
        raise ValueError('server must be a pinned public IPv4 address')
    port = secret['server_port']
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('invalid server_port')
    kind = secret['type']
    if kind not in ('socks', 'http'):
        raise ValueError('type must be socks or http')
    upstream = {key: secret[key] for key in ('type', 'server', 'server_port', 'username', 'password')}
    upstream['tag'] = 'residential'
    if kind == 'socks':
        upstream.update(version='5', network='tcp')
    config = {
        'log': {'level': 'warn'},
        'inbounds': [{'type': 'redirect', 'tag': 'tcp-in', 'listen': '127.0.0.1', 'listen_port': 12345},
                     {'type': 'direct', 'tag': 'dns-in', 'listen': '127.0.0.1', 'listen_port': 1053}],
        'outbounds': [upstream],
        'dns': {'servers': [{'type': 'https', 'tag': 'dns-proxied', 'server': '1.1.1.1',
                             'server_port': 443, 'path': '/dns-query',
                             'tls': {'enabled': True, 'server_name': 'cloudflare-dns.com'},
                             'detour': 'residential'}], 'final': 'dns-proxied',
                'strategy': 'ipv4_only'},
        'route': {'rules': [{'inbound': ['dns-in'], 'action': 'hijack-dns'}], 'final': 'residential'}}
    return config


if __name__ == '__main__':
    secret = json.loads(Path('/run/secrets/proxy.json').read_text())
    config = make_config(secret)
    Path('/run/upstream-ip').write_text(secret['server'])
    Path('/run/upstream-port').write_text(str(secret['server_port']))
    os.umask(0o077)
    output = Path('/run/sing-box.json')
    output.write_text(json.dumps(config))
    os.chown(output, 900, 900)
