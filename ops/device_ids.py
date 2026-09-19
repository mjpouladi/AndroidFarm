"""Shared device identifiers, address pools and loopback ADB allocation."""
from ipaddress import IPv4Address, IPv4Network
import re


EGRESS_POOL = IPv4Network('10.231.0.0/16')
CONTROL_POOL = IPv4Network('10.232.0.0/16')
DEVICE_PREFIX = 29
ADB_PORT_BASE = 5550
MAX_TCP_PORT = 65535

# This is a protocol/addressing ceiling, not a product or license limit.
DEVICE_LIMIT = min(EGRESS_POOL.num_addresses // (1 << (32 - DEVICE_PREFIX)),
                   CONTROL_POOL.num_addresses // (1 << (32 - DEVICE_PREFIX)),
                   MAX_TCP_PORT - ADB_PORT_BASE)


def device_id(index):
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= DEVICE_LIMIT:
        raise ValueError(f'device index must be between 1 and {DEVICE_LIMIT}')
    return f'num{index:02d}'


def device_index(value, aliases=True):
    prefix = r'(?:num|dev)' if aliases else 'num'
    match = re.fullmatch(prefix + r'(0*[1-9]\d*)', value or '')
    if not match:
        raise ValueError('device must use numNN (or devNN where aliases are accepted)')
    index = int(match.group(1))
    if index > DEVICE_LIMIT:
        raise ValueError(f'device exceeds the address/port allocator ceiling ({DEVICE_LIMIT})')
    return index


def canonical_device(value):
    return device_id(device_index(value))


def _subnet(pool, index):
    device_index(device_id(index), aliases=False)
    size = 1 << (32 - DEVICE_PREFIX)
    return IPv4Network((IPv4Address(int(pool.network_address) + (index - 1) * size), DEVICE_PREFIX))


def network_plan(index):
    egress = _subnet(EGRESS_POOL, index)
    control = _subnet(CONTROL_POOL, index)
    return {
        'egress_subnet': str(egress),
        'control_subnet': str(control),
        'proxy_egress_ip': str(egress.network_address + 2),
        'proxy_control_ip': str(control.network_address + 2),
        'screen_control_ip': str(control.network_address + 3),
        'adb_port': ADB_PORT_BASE + index,
        'bridge': f'br-af{index:05d}',
        'iptables_chain': f'AF{index:05d}',
    }
