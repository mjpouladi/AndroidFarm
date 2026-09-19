"""Read-only Linux capacity probe; admission limits match generated Compose budgets."""
import argparse
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time

GIB = 1024 ** 3
CPU_PER_DEVICE = 6  # Android 4 + screen 1.5 + proxy .5
RAM_PER_DEVICE = 5.25
DATA_PER_DEVICE_GIB = 12
MIN_DATA_RESERVE_GIB = 40


def capacity(cpu, ram_gib, policy_limit=None):
    """Return safe simultaneous capacity; policy_limit may only lower it."""
    if cpu <= 0 or ram_gib <= 0:
        return 0, max(2, cpu * .10), max(8, ram_gib * .20)
    reserve_cpu = max(2, cpu * .10)
    reserve_ram = max(8, ram_gib * .20)
    slots = max(0, min(math.floor((cpu - reserve_cpu) / CPU_PER_DEVICE),
                       math.floor((ram_gib - reserve_ram) / RAM_PER_DEVICE)))
    if policy_limit is not None:
        if isinstance(policy_limit, bool) or not isinstance(policy_limit, int) or policy_limit < 0:
            raise ValueError('policy_limit must be a non-negative integer')
        slots = min(slots, policy_limit)
    return slots, reserve_cpu, reserve_ram


def catalog_capacity(total_gib, free_gib, per_device_gib=DATA_PER_DEVICE_GIB):
    """Estimate additional persistent devices from the actual /data filesystem."""
    if total_gib <= 0 or free_gib < 0 or per_device_gib <= 0 or free_gib > total_gib:
        raise ValueError('invalid storage values')
    reserve = max(MIN_DATA_RESERVE_GIB, total_gib * .20)
    return max(0, math.floor((free_gib - reserve) / per_device_gib)), reserve


def _existing_path(path):
    candidate = Path(path)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def local_docker():
    endpoint = subprocess.check_output(
        ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    if not os.environ.get('DOCKER_CONTEXT'):
        endpoint = os.environ.get('DOCKER_HOST', endpoint)
    if endpoint != 'unix:///var/run/docker.sock':
        raise RuntimeError('only the local rootful Docker socket is supported')


def probe(data_root=Path('/opt/farm/data/instances'), policy_limit=None):
    if platform.system() != 'Linux':
        raise RuntimeError('resource detection must run on the Linux Docker host')
    local_docker()
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
    cpu = float(len(os.sched_getaffinity(0)))
    # Walk unified cgroup ancestors; respect inherited quotas as well as leaf limits.
    cg = next((x.split(':', 2)[2] for x in Path('/proc/self/cgroup').read_text().splitlines()
               if x.startswith('0::')), None)
    if cg is None:
        raise RuntimeError('cgroup v2 required; run directly on Ubuntu 22.04/24.04 host')
    total, available = memory['MemTotal'], memory['MemAvailable']
    root = Path('/sys/fs/cgroup')
    leaf = root / cg.lstrip('/')
    if not leaf.is_relative_to(root) or '..' in leaf.parts:
        raise RuntimeError('unexpected cgroup path')
    for node in [leaf, *leaf.parents]:
        if not node.is_relative_to(root):
            break
        quota_file = node / 'cpu.max'
        if quota_file.exists():
            quota, period = quota_file.read_text().split()
            if quota != 'max':
                cpu = min(cpu, int(quota) / int(period))
        limit_file = node / 'memory.max'
        if limit_file.exists() and limit_file.read_text().strip() != 'max':
            limit = int(limit_file.read_text())
            used = int((node / 'memory.current').read_text())
            total, available = min(total, limit), min(available, max(0, limit - used))
    docker_root = subprocess.check_output(
        ['docker', 'info', '--format', '{{.DockerRootDir}}'], text=True).strip()
    storage_path = _existing_path(data_root)
    docker_storage_path = _existing_path(docker_root)
    disk = shutil.disk_usage(storage_path)
    docker_disk = shutil.disk_usage(docker_storage_path)
    stat = os.statvfs(storage_path)
    docker_stat = os.statvfs(docker_storage_path)
    slots, cpu_reserve, ram_reserve = capacity(cpu, total / GIB, policy_limit)
    catalog_slots, disk_reserve = catalog_capacity(disk.total / GIB, disk.free / GIB)
    return dict(schema_version=2, collected_at=int(time.time()), architecture=platform.machine(),
                kernel=platform.release(), cpu_cores=cpu, ram_gib=round(total / GIB, 2),
                available_ram_gib=round(available / GIB, 2), docker_root=docker_root,
                data_root=str(Path(data_root)), storage_probe_path=str(storage_path),
                disk_total_gib=round(disk.total / GIB, 2), disk_free_gib=round(disk.free / GIB, 2),
                disk_reserved_gib=round(disk_reserve, 2), free_inode_ratio=stat.f_favail / stat.f_files if stat.f_files else 1,
                data_disk_total_gib=round(disk.total / GIB, 2),
                data_disk_free_gib=round(disk.free / GIB, 2),
                data_free_inode_ratio=stat.f_favail / stat.f_files if stat.f_files else 1,
                docker_storage_probe_path=str(docker_storage_path),
                docker_disk_total_gib=round(docker_disk.total / GIB, 2),
                docker_disk_free_gib=round(docker_disk.free / GIB, 2),
                docker_free_inode_ratio=(docker_stat.f_favail / docker_stat.f_files
                                         if docker_stat.f_files else 1),
                load_1m=os.getloadavg()[0], capacity=slots, active_capacity=slots,
                catalog_capacity=catalog_slots,
                reserved_cpu=cpu_reserve, reserved_ram_gib=ram_reserve,
                per_device_cpu=CPU_PER_DEVICE, per_device_ram_gib=RAM_PER_DEVICE,
                per_device_disk_gib=DATA_PER_DEVICE_GIB)


def admission(report, active_count):
    reasons = []
    if active_count >= report['capacity']:
        reasons.append('calculated concurrent capacity reached')
    if report['available_ram_gib'] < RAM_PER_DEVICE + report['reserved_ram_gib']:
        reasons.append('insufficient available RAM including host reserve')
    data_total = report.get('data_disk_total_gib', report.get('disk_total_gib', 0))
    data_free = report.get('data_disk_free_gib', report.get('disk_free_gib', 0))
    data_inodes = report.get('data_free_inode_ratio', report.get('free_inode_ratio', 0))
    docker_total = report.get('docker_disk_total_gib', report.get('disk_total_gib', 0))
    docker_free = report.get('docker_disk_free_gib', report.get('disk_free_gib', 0))
    docker_inodes = report.get('docker_free_inode_ratio', report.get('free_inode_ratio', 0))
    if data_free < max(20, data_total * .10):
        reasons.append('persistent data storage below 20 GiB / 10% headroom')
    if docker_free < max(20, docker_total * .10):
        reasons.append('Docker root storage below 20 GiB / 10% headroom')
    if data_inodes < .10:
        reasons.append('persistent data storage has less than 10% free inodes')
    if docker_inodes < .10:
        reasons.append('Docker root storage has less than 10% free inodes')
    if report['load_1m'] > report['cpu_cores'] * 1.2:
        reasons.append('host load too high; retry later')
    if reasons:
        raise RuntimeError('; '.join(reasons))


def catalog_admission(report):
    if report.get('catalog_capacity', 0) < 1:
        raise RuntimeError('persistent data storage has no safe capacity for another device')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(probe(), indent=2))
