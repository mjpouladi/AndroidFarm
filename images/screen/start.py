import os
import signal
import subprocess
import time

children = []
target = os.environ['ADB_TARGET']
width = int(os.environ.get('SCREEN_WIDTH', '720'))
height = int(os.environ.get('SCREEN_HEIGHT', '1280'))
fps = int(os.environ.get('SCREEN_FPS', '20'))
if not 320 <= width <= 4320 or not 320 <= height <= 4320 or not 10 <= fps <= 120:
    raise SystemExit('invalid screen geometry')


def spawn(args):
    child = subprocess.Popen(args)
    children.append(child)
    return child


def stop(*_):
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
spawn(['Xvfb', ':99', '-screen', '0', f'{width}x{height}x24', '-nolisten', 'tcp'])
time.sleep(2)
spawn(['x11vnc', '-display', ':99', '-localhost', '-rfbport', '5900', '-nopw', '-forever', '-shared'])
spawn(['websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900'])
infrastructure = list(children)
scrcpy = None
while True:
    if any(p.poll() is not None for p in infrastructure):
        stop()
    if scrcpy is None or scrcpy.poll() is not None:
        try:
            subprocess.run(['adb', 'connect', target], timeout=15, check=False)
            ready = subprocess.run(['adb', '-s', target, 'shell', 'getprop', 'sys.boot_completed'],
                                   capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            time.sleep(5)
            continue
        if ready.stdout.strip() == b'1':
            if scrcpy is not None:
                children.remove(scrcpy)
            scrcpy = spawn(['scrcpy', '-s', target, '--max-size', str(max(width, height)),
                            '--max-fps', str(fps),
                            '--bit-rate', '2M', '--window-title', os.environ['DEVICE_ID'],
                            '--window-borderless', '--window-x', '0', '--window-y', '0'])
    time.sleep(5)
