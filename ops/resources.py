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


def capacity(cpu, ram_gib):
    reserve_cpu = max(2, cpu * .10)
    reserve_ram = max(8, ram_gib * .20)
    slots = max(0, min(10, math.floor((cpu - reserve_cpu) / CPU_PER_DEVICE),
                       math.floor((ram_gib - reserve_ram) / RAM_PER_DEVICE)))
    return slots, reserve_cpu, reserve_ram


def local_docker():
    endpoint = subprocess.check_output(
        ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    if not os.environ.get('DOCKER_CONTEXT'):
        endpoint = os.environ.get('DOCKER_HOST', endpoint)
    if endpoint != 'unix:///var/run/docker.sock':
        raise RuntimeError('only the local rootful Docker socket is supported')


def probe():
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
    disk = shutil.disk_usage(docker_root)
    stat = os.statvfs(docker_root)
    slots, cpu_reserve, ram_reserve = capacity(cpu, total / GIB)
    return dict(schema_version=1, collected_at=int(time.time()), architecture=platform.machine(),
                kernel=platform.release(), cpu_cores=cpu, ram_gib=round(total / GIB, 2),
                available_ram_gib=round(available / GIB, 2), docker_root=docker_root,
                disk_total_gib=round(disk.total / GIB, 2), disk_free_gib=round(disk.free / GIB, 2),
                free_inode_ratio=stat.f_favail / stat.f_files if stat.f_files else 1,
                load_1m=os.getloadavg()[0], capacity=slots,
                reserved_cpu=cpu_reserve, reserved_ram_gib=ram_reserve,
                per_device_cpu=CPU_PER_DEVICE, per_device_ram_gib=RAM_PER_DEVICE)


def admission(report, active_count):
    reasons = []
    if active_count >= report['capacity']:
        reasons.append('calculated concurrent capacity reached')
    if report['available_ram_gib'] < RAM_PER_DEVICE + report['reserved_ram_gib']:
        reasons.append('insufficient available RAM including host reserve')
    if report['disk_free_gib'] < max(20, report['disk_total_gib'] * .10):
        reasons.append('Docker storage below 20 GiB / 10% headroom')
    if report['free_inode_ratio'] < .10:
        reasons.append('less than 10% free inodes')
    if report['load_1m'] > report['cpu_cores'] * 1.2:
        reasons.append('host load too high; retry later')
    if reasons:
        raise RuntimeError('; '.join(reasons))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(probe(), indent=2))
