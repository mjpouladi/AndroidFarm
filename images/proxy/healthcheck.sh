#!/bin/sh
set -eu
# This runs as root, NOT UID 900: it must traverse transparent TCP + DNS.
if [ -f /run/direct ]; then
    # Direct host egress: the sidecar runs no tunnel. Once Android has booted it
    # owns the namespace's routing policy and the sidecar's own probe is no
    # longer meaningful; adbd listening on 5555 is the liveness signal. Before
    # boot the egress probe below still attests the host path.
    if python3 -c 'import socket; s = socket.create_connection(("127.0.0.1", 5555), timeout=3); s.close()' 2>/dev/null; then
        exit 0
    fi
fi
curl --noproxy '*' -4 -fsS --max-time 8 https://api.ipify.org >/dev/null
