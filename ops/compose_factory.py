"""Render a single managed instance from the audited farm topology."""
import copy
from pathlib import Path
import re

from generate_farm import generate

PROFILES = {
    'phone_hd': {'width': 720, 'height': 1280, 'dpi': 240, 'fps': 20,
                 'model': 'Redroid QA Phone HD'},
    'phone_fhd': {'width': 1080, 'height': 1920, 'dpi': 420, 'fps': 20,
                  'model': 'Redroid QA Phone FHD'},
    'tablet_landscape': {'width': 1280, 'height': 800, 'dpi': 240, 'fps': 20,
                         'model': 'Redroid QA Tablet'},
}


def canonical_device(value):
    match = re.fullmatch(r'(?:num|dev)(0[1-9]|[1-9][0-9]|1[0-9]{2}|200)', value or '')
    if not match:
        raise ValueError('device must be num01..num200 or dev01..dev200')
    return f'num{int(match.group(1)):02d}'


def single_instance(device, profile='phone_hd', data_root=Path('/opt/farm/data/instances')):
    device = canonical_device(device)
    if profile not in PROFILES:
        raise ValueError('unknown QA profile: ' + profile)
    index = int(device[3:])
    full = generate(index)
    keep_services = {'farm-anchor', f'proxy-{device}', f'android-{device}', f'screen-{device}'}
    keep_networks = {'coolify', f'egress-{device}', f'control-{device}'}
    document = copy.deepcopy(full)
    document['name'] = f'android-farm-{device}'
    document['services'] = {key: value for key, value in document['services'].items() if key in keep_services}
    document['networks'] = {key: value for key, value in document['networks'].items() if key in keep_networks}
    document['secrets'] = {f'proxy-{device}': full['secrets'][f'proxy-{device}']}
    document['volumes'] = {}
    android = document['services'][f'android-{device}']
    target = Path(data_root).resolve() / device / 'data'
    if target == Path('/') or not target.is_absolute():
        raise ValueError('data root must resolve to a safe absolute path')
    android['volumes'] = [{'type': 'bind', 'source': str(target), 'target': '/data',
                           'bind': {'create_host_path': False}}]
    selected = PROFILES[profile]
    android['command'] = [
        f'androidboot.redroid_width={selected["width"]}',
        f'androidboot.redroid_height={selected["height"]}',
        f'androidboot.redroid_dpi={selected["dpi"]}',
        f'androidboot.redroid_fps={selected["fps"]}',
        'androidboot.redroid_gpu_mode=guest',
        'androidboot.use_memfd=true',
        'androidboot.redroid_dns=1.1.1.1',
        f'androidboot.serialno=farm-{device}',
        'ro.product.brand=redroid',
        'ro.product.manufacturer=remote-android',
        f'ro.product.model={selected["model"]}',
    ]
    android['labels']['farm.qa-profile'] = profile
    return document
