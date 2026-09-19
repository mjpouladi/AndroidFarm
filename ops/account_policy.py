"""Local manual safety hold; no vendor status scraping or automated appeals."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

DEFAULT = Path('/var/lib/android-farm/holds.json')


def _read_holds(path):
    try:
        from .secureio import read_private_json, require_private_directory
    except ImportError:
        from secureio import read_private_json, require_private_directory
    if path.parent.exists():
        require_private_directory(path.parent, 'safety hold directory')
    if not path.exists():
        return {}
    value = read_private_json(path, 'safety holds')
    if not isinstance(value, dict):
        raise RuntimeError('safety holds file is invalid')
    return value


def assert_not_held(device, path=DEFAULT):
    if device in _read_holds(path):
        raise RuntimeError('device on manual safety hold; review restriction through official channels before release')


def main():
    try:
        from .secureio import atomic_json, require_private_directory
        from . import farmctl
    except ImportError:
        from secureio import atomic_json, require_private_directory
        import farmctl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['hold', 'release', 'list'])
    parser.add_argument('device', nargs='?')
    parser.add_argument('--reason', choices=['account-restriction', 'ip-change', 'ownership-review', 'maintenance'])
    parser.add_argument('--review-completed', action='store_true')
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('root required on Ubuntu host')
    import fcntl
    # A hold must be persisted even while a long backup owns the lifecycle
    # lock. It has a dedicated tiny lock, then cuts egress before waiting for
    # normal Docker shutdown.
    with open('/run/lock/android-farm-holds.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        holds = _read_holds(DEFAULT)
        if args.action == 'list':
            print(json.dumps(holds, indent=2))
            return
        if not re.fullmatch(r'num(?:0[1-9]|[1-9][0-9]|1[0-9]{2}|200)', args.device or ''):
            parser.error('valid device required')
        require_private_directory(DEFAULT.parent, 'safety hold directory', create=True)
        if args.action == 'hold':
            if not args.reason:
                parser.error('--reason required')
            holds[args.device] = dict(reason=args.reason, at=int(time.time()))
            atomic_json(DEFAULT, holds)  # Persist even if Docker stop fails.
        else:
            if not args.review_completed:
                parser.error('explicit --review-completed required; no timed automatic release')
            holds.pop(args.device, None)
            atomic_json(DEFAULT, holds)
    if args.action == 'hold':
        # The persisted hold makes a concurrent start fail on its next guard
        # check. Close the network boundary first, then perform graceful stop.
        try:
            farmctl.cut_egress(args.device)
        finally:
            farmctl.stop(args.device)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
