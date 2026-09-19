#!/bin/sh
set -eu
# This runs as root, NOT UID 900: it must traverse transparent TCP + DNS.
curl --noproxy '*' -4 -fsS --max-time 8 https://api.ipify.org >/dev/null
