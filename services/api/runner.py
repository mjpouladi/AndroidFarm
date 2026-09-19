"""Linux process-tree supervisor for a fixed, reviewed host CLI command.

The API starts this helper in a new session. If the Gunicorn worker disappears,
Linux delivers SIGTERM here even though the helper owns a separate session.
All CLI descendants inherit that process group and receive bounded shutdown.
No ``preexec_fn`` runs inside the multithreaded API process.
"""
import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time


def supervise(command, parent_pid, *, grace_seconds=2):
    if sys.platform != 'linux':
        raise RuntimeError('process supervision requires Linux')
    # Refuse to signal an inherited shell/API group if this helper is launched
    # without the new session required by bounded_process.
    if os.getpgrp() != os.getpid() or os.getsid(0) != os.getpid():
        raise RuntimeError('supervisor must own a dedicated process session')

    def stop_tree(_signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        # Even a child that ignores TERM cannot outlive this deadline. Killing
        # the entire group includes this helper; normal completion never calls
        # this path and therefore does not signal unrelated processes.
        time.sleep(grace_seconds)
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, stop_tree)
    signal.signal(signal.SIGINT, stop_tree)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise RuntimeError('could not enable Linux parent-death supervision')
    # A worker can die between Popen and prctl; the signal is not retroactive.
    # Checking the expected worker PID closes that startup race before launch.
    if os.getppid() != parent_pid:
        return 125
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    code = child.wait()
    return code if code >= 0 else 128 - code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.parent_pid <= 1 or not command:
        parser.error('a live parent PID and an explicit command are required')
    try:
        return supervise(command, args.parent_pid)
    except (OSError, RuntimeError):
        # Command arguments may refer to private request files; don't echo them.
        print('host process supervisor could not initialize', file=sys.stderr)
        return 125


if __name__ == '__main__':
    sys.exit(main())
