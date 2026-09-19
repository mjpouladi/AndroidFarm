#!/usr/bin/env python3
"""Non-destructive Ubuntu preparation. Default is plan-only; --apply installs prerequisites."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import urllib.request


def run(*args):
    subprocess.run(args, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    if platform.system() != 'Linux':
        p.error('run on the target Ubuntu host')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') not in ('22.04', '24.04'):
        p.error('only Ubuntu 22.04 and 24.04 supported')
    if Path('/.dockerenv').exists():
        p.error('run on the host, not inside a container')
    print(json.dumps({'mode': 'apply' if args.apply else 'plan', 'architecture': platform.machine(),
                      'docker_present': bool(shutil.which('docker')),
                      'actions': ['install prerequisite packages', 'install Docker only if absent',
                                  'check binder kernel support', 'create private state directories'],
                      'preserves': ['OS', 'disks', 'existing Coolify', 'Docker configuration', 'existing volumes']}, indent=2))
    if not args.apply:
        return
    if os.geteuid() != 0:
        p.error('--apply requires root')
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'ca-certificates', 'curl', 'git', 'python3', 'iptables',
        'apache2-utils', 'apksigner', 'aapt', 'rsync', 'restic')
    if not shutil.which('docker'):
        conflicts = []
        for package in ('docker.io', 'docker-compose', 'containerd', 'runc', 'podman-docker'):
            found = subprocess.run(['dpkg-query', '-W', '-f=${Status}', package], capture_output=True, text=True)
            if found.returncode == 0 and found.stdout == 'install ok installed':
                conflicts.append(package)
        if conflicts:
            raise RuntimeError('existing container packages require operator migration: ' + ', '.join(conflicts))
        key = Path('/etc/apt/keyrings/docker.asc')
        key.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen('https://download.docker.com/linux/ubuntu/gpg', timeout=30) as response:
            key.write_bytes(response.read())
        key.chmod(0o644)
        arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
        codename = release['VERSION_CODENAME'].strip('"')
        source = Path('/etc/apt/sources.list.d/android-farm-docker.sources')
        source.write_text(f'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: {codename}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {key}\n')
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin')
        run('systemctl', 'enable', '--now', 'docker')
    run('docker', 'info', '--format', '{{.ServerVersion}}')
    version = subprocess.check_output(['docker', 'compose', 'version', '--short'], text=True).strip().lstrip('v').split('-')[0]
    if tuple(map(int, version.split('.'))) < (2, 33, 1):
        raise RuntimeError('Compose >= 2.33.1 required; upgrade Docker through your existing package source')
    run('modprobe', 'binder_linux', 'devices=binder,hwbinder,vndbinder')
    if not Path('/dev/binder').exists() and not Path('/dev/binderfs/binder-control').exists():
        raise RuntimeError('binder device unavailable: follow Redroid kernel setup; no kernel replacement or reboot performed')
    Path('/etc/modules-load.d/android-farm.conf').write_text('binder_linux\n')
    Path('/etc/modprobe.d/android-farm.conf').write_text('options binder_linux devices=binder,hwbinder,vndbinder\n')
    for path in ('/etc/android-farm/secrets', '/var/lib/android-farm', '/var/backups/android-farm',
                 '/opt/farm/data/instances'):
        folder = Path(path)
        if folder.is_symlink():
            raise RuntimeError('unexpected symlink: ' + path)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    print('Host prepared. Deploy the reviewed release in Coolify, install device-provisioner, then use device-provisioner up --request for the first device.')


if __name__ == '__main__':
    main()
