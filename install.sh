#!/bin/sh
# Safe root launcher for the reviewed Android Farm QA quickstart.
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022

OFFICIAL_REPOSITORY='https://github.com/mjpouladi/AndroidFarm.git'
SOURCE_ROOT='/opt/android-farm'
SOURCE_DIR="$SOURCE_ROOT/source"

fail() {
    printf '%s\n' "install.sh: $*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail 'run this launcher as root'
[ -r /etc/os-release ] || fail '/etc/os-release is unavailable'

# /etc/os-release is an Ubuntu-owned data file on the target host.
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = 'ubuntu' ] || fail 'only Ubuntu is supported'
case "${VERSION_ID:-}" in
    22.04|24.04) ;;
    *) fail 'only Ubuntu 22.04 and 24.04 are supported' ;;
esac

missing_packages=''
command -v git >/dev/null 2>&1 || missing_packages="$missing_packages git"
command -v python3 >/dev/null 2>&1 || missing_packages="$missing_packages python3"
if ! dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null \
        | grep -qx 'install ok installed'; then
    missing_packages="$missing_packages ca-certificates"
fi
if [ -n "$missing_packages" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    # Values in missing_packages come only from the fixed allowlist above.
    # shellcheck disable=SC2086
    apt-get install -y --no-install-recommends $missing_packages
fi

[ ! -L /opt ] || fail '/opt may not be a symbolic link'
[ ! -L "$SOURCE_ROOT" ] || fail "$SOURCE_ROOT may not be a symbolic link"
install -d -o root -g root -m 0755 "$SOURCE_ROOT"

if [ -L "$SOURCE_DIR" ]; then
    fail "$SOURCE_DIR may not be a symbolic link"
elif [ -e "$SOURCE_DIR" ]; then
    [ -d "$SOURCE_DIR/.git" ] || fail "$SOURCE_DIR exists but is not a Git checkout"

    unsafe_path=$(find "$SOURCE_DIR" -xdev \( ! -user root -o -perm /022 \) -print -quit)
    [ -z "$unsafe_path" ] || fail "checkout has non-root-owned or writable content: $unsafe_path"
    linked_path=$(find "$SOURCE_DIR" -xdev -type l -print -quit)
    [ -z "$linked_path" ] || fail "checkout contains a symbolic link: $linked_path"

    origin_url=$(git -c core.hooksPath=/dev/null -C "$SOURCE_DIR" remote get-url origin)
    [ "$origin_url" = "$OFFICIAL_REPOSITORY" ] || fail 'origin is not the official AndroidFarm repository'
    current_branch=$(git -c core.hooksPath=/dev/null -C "$SOURCE_DIR" symbolic-ref --quiet --short HEAD) \
        || fail 'checkout is detached; expected branch main'
    [ "$current_branch" = 'main' ] || fail "checkout branch is $current_branch; expected main"
    [ -z "$(git -c core.hooksPath=/dev/null -C "$SOURCE_DIR" status --porcelain=v1 --untracked-files=all)" ] \
        || fail 'checkout has local changes; preserve or commit them before updating'

    git -c core.hooksPath=/dev/null -C "$SOURCE_DIR" fetch --no-tags origin \
        refs/heads/main:refs/remotes/origin/main
    git -c core.hooksPath=/dev/null -C "$SOURCE_DIR" merge-base --is-ancestor \
        HEAD refs/remotes/origin/main \
        || fail 'local main has commits or divergence; refusing to overwrite it'
    git -c core.hooksPath=/dev/null -C "$SOURCE_DIR" merge --ff-only --no-edit \
        refs/remotes/origin/main
else
    git -c core.hooksPath=/dev/null clone --branch main --single-branch \
        "$OFFICIAL_REPOSITORY" "$SOURCE_DIR"
fi

# A reviewed source tree does not need symlinks. Refuse them after clone too,
# before executing Python as root.
linked_path=$(find "$SOURCE_DIR" -xdev -type l -print -quit)
[ -z "$linked_path" ] || fail "checkout contains a symbolic link: $linked_path"
[ ! -L "$SOURCE_DIR/installer" ] || fail 'installer directory may not be a symbolic link'
[ ! -L "$SOURCE_DIR/installer/quickstart.py" ] \
    || fail 'installer/quickstart.py may not be a symbolic link'
[ -f "$SOURCE_DIR/installer/quickstart.py" ] \
    || fail 'the reviewed checkout does not contain installer/quickstart.py'

exec python3 "$SOURCE_DIR/installer/quickstart.py" "$@"
