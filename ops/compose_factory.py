"""Render one managed instance from the audited farm topology."""

from generate_farm import generate_one
from .device_ids import canonical_device

PROFILES = {
    'phone_hd': {'width': 720, 'height': 1280, 'dpi': 240, 'fps': 20,
                 'model': 'Redroid QA Phone HD'},
    'phone_fhd': {'width': 1080, 'height': 1920, 'dpi': 420, 'fps': 20,
                  'model': 'Redroid QA Phone FHD'},
    'tablet_landscape': {'width': 1280, 'height': 800, 'dpi': 240, 'fps': 20,
                         'model': 'Redroid QA Tablet'},
}


def single_instance(device, profile='phone_hd'):
    device = canonical_device(device)
    if profile not in PROFILES:
        raise ValueError('unknown QA profile: ' + profile)
    index = int(device[3:])
    document = generate_one(index)
    android = document['services'][f'android-{device}']
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
    screen = document['services'][f'screen-{device}']
    screen['environment'].update({
        'SCREEN_WIDTH': str(selected['width']),
        'SCREEN_HEIGHT': str(selected['height']),
        'SCREEN_FPS': str(selected['fps']),
    })
    return document
