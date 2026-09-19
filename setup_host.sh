#!/usr/bin/env bash
set -Eeuo pipefail

mode="${1:---check}"
if [[ "${mode}" != "--check" && "${mode}" != "--apply" ]]; then
  echo "usage: sudo ./setup_host.sh [--check|--apply]" >&2
  exit 2
fi

if [[ ! -r /etc/os-release ]]; then
  echo "unsupported host: /etc/os-release is missing" >&2
  exit 1
fi
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || ( "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" ) ]]; then
  echo "only Ubuntu 22.04 and 24.04 are supported" >&2
  exit 1
fi

if [[ "${mode}" == "--apply" ]]; then
  if [[ "${EUID}" -ne 0 ]]; then
    echo "--apply must run as root" >&2
    exit 1
  fi
  modprobe binder_linux devices=binder,hwbinder,vndbinder
  if modinfo ashmem_linux >/dev/null 2>&1; then
    modprobe ashmem_linux
  else
    echo "ashmem_linux is unavailable; generated Compose enables androidboot.use_memfd=true" >&2
  fi
  install -d -m 0755 /etc/modules-load.d /etc/modprobe.d
  printf 'binder_linux\n' > /etc/modules-load.d/android-farm.conf
  printf 'options binder_linux devices=binder,hwbinder,vndbinder\n' > /etc/modprobe.d/android-farm.conf
  install -d -m 0700 /etc/android-farm/secrets /var/lib/android-farm /var/backups/android-farm /opt/farm/data/instances
fi

if ! grep -qw binder /proc/filesystems && ! grep -qw binder_linux /proc/modules; then
  echo "binder_linux is not loaded" >&2
  exit 1
fi
if [[ ! -e /dev/binder && ! -e /dev/binderfs/binder-control ]]; then
  echo "binder filesystem is present but binder devices are unavailable" >&2
  exit 1
fi
if command -v docker >/dev/null 2>&1; then
  docker version --format 'Docker server {{.Server.Version}}'
  docker compose version
else
  echo "Docker is not installed; run installer/bootstrap.py --apply first" >&2
  exit 1
fi
if ! command -v iptables-restore >/dev/null 2>&1; then
  echo "iptables-restore is required for the atomic host egress guard" >&2
  exit 1
fi
echo "Redroid host prerequisites passed"
